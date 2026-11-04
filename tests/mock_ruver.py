"""Виртуальная камера «Рувер»: HTTP API по карте `ruver_api_map.json`.

Ответы строятся из схем карты, поэтому виртуальная камера не отстаёт от неё. Поля-переключатели
задают сбои, которые проверка должна уметь различать. Журнал `requests` показывает, что реально
ушло в сеть, — на него опираются тесты табу и разрешений.
"""

import contextlib
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from qa_tool.drivers.camera.ruver.client import READ, WRITE, load_map

MAX_SESSIONS = 3


def sample(schema: dict | None) -> Any:
    """Значение, подходящее под схему; числа — внутри пределов min/max виртуальной камеры."""
    schema = schema or {}
    if "enum" in schema:
        return schema["enum"][0]
    if "allOf" in schema:
        merged: dict = {}
        for part in schema["allOf"]:
            merged |= sample(part)
        return merged
    if "oneOf" in schema or "anyOf" in schema:
        return sample((schema.get("oneOf") or schema.get("anyOf"))[0])
    kind = schema.get("type")
    if kind == "object":
        return {key: sample(value) for key, value in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [sample(schema["items"])] if "items" in schema else []
    return {"integer": 50, "number": 50, "boolean": False}.get(kind, "x")


@dataclass
class Logged:
    """Запрос, дошедший до виртуальной камеры."""

    method: str
    path: str
    query: str
    body: Any
    session: str


@dataclass
class MockRuver:
    """Настройки сбоев; `start()` до первого запроса, `stop()` в конце."""

    password: str = "admin"
    username: str = "admin"
    busy_sessions: int = 0  # сеансы, занятые кем-то другим
    missing: set[str] = field(default_factory=set)  # адреса с ответом 404
    corrupt: set[str] = field(default_factory=set)  # ответ без обязательных полей
    forbidden: set[str] = field(default_factory=set)  # ответ 403
    slow: set[str] = field(default_factory=set)  # ответ позже тайм-аута клиента
    slow_s: float = 0.6
    reboot_needed: set[str] = field(default_factory=set)
    ignored_writes: set[str] = field(default_factory=set)  # отвечает 200, значение не меняет
    stuck: set[str] = field(default_factory=set)  # первая запись проходит, возврат — нет
    side_effects: dict[str, tuple[str, str, Any]] = field(default_factory=dict)
    # адрес записи → (адрес, поле, значение), которое он меняет рядом
    accepts_out_of_range: bool = False
    refuses_next_write_once: set[str] = field(default_factory=set)  # занята после смены значения
    restarts_on_export: bool = False  # как прошивка 0.6.14: экспорт перезапускает службу API
    down_s: float = 0.15
    expire_session_after: int = 0  # сеанс закрывается по времени после N запросов
    on_request: Callable[["MockRuver", Logged], None] | None = None
    requests: list[Logged] = field(default_factory=list)
    login_attempts: int = 0
    sessions: dict[str, bool] = field(default_factory=dict)  # сеанс → вошёл ли пользователь
    closed_sessions: list[str] = field(default_factory=list)
    reboots: int = 0

    def __post_init__(self) -> None:
        self.api = load_map()
        self.state: dict[str, Any] = {}
        for op in self.api.operations:
            if op.kind == READ and op.method == "GET" and not op.binary:
                self.state[op.path] = sample(op.schema)
        for value in self.state.values():
            if isinstance(value, dict):
                for name in value:
                    if name.endswith("Range"):
                        value[name] = {"min": 0, "max": 100}
        hardware = self.state["/api/hardware"]
        hardware |= {name: True for name in hardware if name.endswith("Support")}
        self._down_until = 0.0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._served = 0
        self._stuck_written: set[str] = set()
        self.busy_after_write: set[str] = set()

    @property
    def port(self) -> int:
        """Порт HTTP виртуальной камеры."""
        return self._server.server_address[1]

    def start(self) -> "MockRuver":
        """Начать отвечать на случайном порту."""
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        """Остановить сервер."""
        self._server.shutdown()
        self._server.server_close()

    def sent(self, method: str, path: str) -> list[Logged]:
        """Запросы с этим методом и адресом, дошедшие до камеры."""
        return [r for r in self.requests if r.method == method and r.path == path]

    def field_value(self, path: str, name: str) -> Any:
        """Текущее значение поля настройки."""
        return self.state[path][name]

    # --- ответы -----------------------------------------------------------------------------

    def _create_session(self) -> tuple[int, Any]:
        if len(self.sessions) + self.busy_sessions >= MAX_SESSIONS:
            return 409, {"errorCode": 4001, "errorDescription": "Session count exceeded"}
        session = str(uuid.uuid4())
        self.sessions[session] = False
        return 200, {
            "sessionID": session,
            "maxConnectionsAllowed": MAX_SESSIONS,
            "connectionsUsed": len(self.sessions) + self.busy_sessions,
        }

    def _login(self, session: str, body: dict) -> tuple[int, Any]:
        self.login_attempts += 1
        if session not in self.sessions:
            return 401, {"errorCode": 2001, "errorDescription": "Bad session"}
        expected = hashlib.md5(f"{self.password}:{session}".encode()).hexdigest()
        if body.get("username") != self.username or body.get("password") != expected:
            return 401, {"errorCode": 2002, "errorDescription": "Bad credentials"}
        self.sessions[session] = True
        return 200, {"userRole": 2}

    def _write(self, op, body: dict) -> tuple[int, Any]:
        state = self.state[op.path]
        value = body.get(op.setting)
        if op.values and value not in op.values:
            return 400, {"errorCode": 1003, "errorDescription": "Bad parameter"}
        limits = state.get(op.range_field) if op.range_field else None
        if limits and not self.accepts_out_of_range and not limits["min"] <= value <= limits["max"]:
            return 400, {"errorCode": 1003, "errorDescription": "Bad parameter"}
        if op.path in self.ignored_writes:
            return 200, None
        if op.path in self.busy_after_write:
            self.busy_after_write.discard(op.path)
            return 400, {"errorCode": 1003, "errorDescription": "Busy"}
        if op.path in self.refuses_next_write_once:
            self.refuses_next_write_once.discard(op.path)
            self.busy_after_write.add(op.path)
        if op.path in self.stuck and op.path in self._stuck_written:
            return 200, None
        self._stuck_written.add(op.path)
        state[op.setting] = value
        if op.path in self.side_effects:
            path, name, side_value = self.side_effects.pop(op.path)
            self.state[path][name] = side_value
        return 200, None

    def answer(self, method: str, path: str, session: str, body: Any) -> tuple[int, Any]:
        """Ответ камеры: (код, тело): JSON, байты или None — пустое тело."""
        if (method, path) == ("GET", "/api/auth/create_session"):
            return self._create_session()
        if (method, path) == ("POST", "/api/auth/login"):
            return self._login(session, body or {})
        if (method, path) == ("POST", "/api/auth/logout"):
            self.sessions.pop(session, None)
            self.closed_sessions.append(session)
            return 200, None
        if path in self.missing or not path.startswith("/api/"):
            return 404, {"errorCode": 1, "errorDescription": "Not found"}
        if not self.sessions.get(session):
            return 401, {"errorCode": 2001, "errorDescription": "Bad session"}
        self._served += 1
        if self.expire_session_after and self._served == self.expire_session_after:
            self.sessions.pop(session, None)
            return 401, {"errorCode": 105, "errorDescription": "Session expired"}
        if path in self.forbidden:
            return 403, {"errorCode": 5, "errorDescription": "Forbidden"}
        op = self.api.get(method, path)
        if op is None:
            return 404, {"errorCode": 1, "errorDescription": "Not found"}
        if path == "/api/service/need_reboot":
            return 200, {"needReboot": (body or {}).get("route") in self.reboot_needed}
        if path == "/api/service/export_settings" and self.restarts_on_export:
            self._down_until = time.monotonic() + self.down_s
            self.sessions.clear()
            return 200, b"settings"
        if path == "/api/service/reboot":
            self.reboots += 1
            self.sessions.clear()
            return 200, None
        if op.kind == WRITE:
            return self._write(op, body or {})
        if op.binary:
            return 200, b"\xff\xd8binary"
        if path in self.corrupt:
            return 200, {}
        return 200, self.state.get(path)

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                # чужой запрос (ONVIF на этот же порт) разбирать незачем: он получит 404
                body = json.loads(raw) if raw and parts.path.startswith("/api/") else None
                session = self.headers.get("X-Session-ID") or ""
                logged = Logged(self.command, parts.path, parts.query, body, session)
                if time.monotonic() < mock._down_until:
                    self.close_connection = True  # служба перезапускается: ответа нет
                    return
                with mock._lock:
                    mock.requests.append(logged)
                    if mock.on_request:
                        mock.on_request(mock, logged)
                    status, payload = mock.answer(self.command, parts.path, session, body)
                if parts.path in mock.slow:
                    time.sleep(mock.slow_s)
                data = (
                    payload
                    if isinstance(payload, bytes)
                    else b""
                    if payload is None
                    else json.dumps(payload).encode()
                )
                self.send_response(status)
                kind = (
                    "application/octet-stream" if isinstance(payload, bytes) else "application/json"
                )
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                # клиент ушёл по тайм-ауту: для медленных адресов это и проверяется
                with contextlib.suppress(OSError):
                    self.wfile.write(data)

            do_GET = _serve  # noqa: N815 — имена задаёт http.server
            do_POST = _serve  # noqa: N815

            def log_message(self, *args):
                pass

        return Handler
