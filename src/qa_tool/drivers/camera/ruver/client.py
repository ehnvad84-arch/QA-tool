"""Адаптер API камеры «Рувер»: карта операций и сеанс (AD-4).

К камере клиент ходит только через `drivers/camera/http.request`: там стоит табу (AD-7). Сверх него
здесь второй рубеж: операция вне карты и изменяющая операция без разрешения сценария
отказываются до отправки.

Камера держит не больше 3 сеансов, а неверный пароль расходует счётчик попыток и может заблокировать
вход. Поэтому сеанс один на проверку и закрывается при любом исходе, а отказ в пароле не
повторяется никогда. Повторный вход допустим один раз и только после успешного первого: камера
сама закрыла сеанс по времени, пароль уже подтверждён.

Входов у прошивки два: MD5 от «пароль:сеанс» (заводская прошивка и описание API 0.6.7_p4) и
PBKDF2 с вызовом `POST /api/auth/challenge` (прошивка своей сборки с 04.11.2026; лёгкая `rw-camd`
знает только его и каждый MD5-вход считает неудачей). Какой вход у камеры, клиент узнаёт по ответу
на вызов: он ничего не меняет и попыткой не считается. Пробовать «сначала MD5, потом PBKDF2» нельзя:
лишняя попытка тратит счётчик, общий для API, ONVIF и RTSP.

Класс не потокобезопасен: один клиент — одна проверка в рабочем процессе камеры.
"""

import contextlib
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from qa_tool.drivers.camera import http

READ, WRITE, TABOO, DESTRUCTIVE, NOT_CALLED, RETURN_ONLY = (
    "чтение",
    "запись",
    "табу",
    "разрушающая",
    "не вызывается",
    "только возврат",
)
MAP_FILE = Path(__file__).resolve().parents[3] / "data" / "ruver_api_map.json"
SESSION_HEADER = "X-Session-ID"
CREATE_SESSION = "/api/auth/create_session"
LOGIN = "/api/auth/login"
CHALLENGE = "/api/auth/challenge"
CHALLENGE_ALGORITHM = "pbkdf2-sha256"
# Прошивка отдаёт 10000. Предел — от камеры с чужим ответом: миллиард итераций занял бы
# рабочий процесс на часы, а вход всё равно не состоялся бы.
MAX_ITERATIONS = 1_000_000
_SALT = re.compile(r"[0-9a-fA-F]{32}")
LOGOUT = "/api/auth/logout"
# Неверный или истёкший сеанс, а не отказ в правах. 1006, 1013 и 2001 — из описания API;
# 105 («Session expired») описание не называет, его отдаёт прошивка 0.6.14 (живая камера TMID-018).
SESSION_GONE_CODES = frozenset({105, 1006, 1013, 2001})


class RuverError(Exception):
    """Сбой работы с API «Рувер»; в протокол идёт как ошибка стенда или пропуск."""


class LoginRejected(RuverError):
    """Камера не приняла логин или пароль. Повторять нельзя: расходуются попытки входа."""


class NoFreeSession(RuverError):
    """На камере заняты все сеансы."""


class LoginFailed(RuverError):
    """Вход не состоялся не из-за пароля: неожиданный ответ или блокировка попыток."""


class CallRefused(RuverError):
    """Вызов отказан до отправки: операции нет в карте или сценарий её не разрешил."""


@dataclass(frozen=True)
class Operation:
    """Одна операция карты API; поля — как в `ruver_api_map.json`."""

    id: str
    method: str
    path: str
    title: str
    kind: str
    reason: str = ""
    query: dict[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    schema: dict[str, Any] | None = None
    binary: bool = False
    sensitive: bool = False
    live: bool = False
    body: dict[str, Any] | None = None
    setting: str = ""
    boolean: bool = False
    values: tuple[Any, ...] = ()
    range_field: str = ""


@dataclass(frozen=True)
class ApiMap:
    """Карта API: версия описания и операции в порядке описания."""

    version: str
    operations: tuple[Operation, ...]

    def get(self, method: str, path: str) -> Operation | None:
        """Операция по методу и адресу; None — такой в карте нет."""
        return next((op for op in self.operations if op.method == method and op.path == path), None)


@cache
def load_map() -> ApiMap:
    """Карта из файла программы; читается один раз на процесс."""
    data = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    operations = tuple(
        Operation(
            **{
                **item,
                "missing": tuple(item.get("missing", ())),
                "values": tuple(item.get("values", ())),
            }
        )
        for item in data["operations"]
    )
    return ApiMap(data["version"], operations)


def password_hash(password: str, session_id: str) -> str:
    """Пароль для входа по описанию API: MD5 от «пароль:идентификатор сеанса»."""
    text = f"{password}:{session_id}".encode()
    return hashlib.md5(text, usedforsecurity=False).hexdigest()


def challenge_password(password: str, session_id: str, salt: str, iterations: int) -> str:
    """Пароль для входа по вызову: HMAC-SHA256 сеанса ключом PBKDF2-SHA256 от пароля.

    Так считают веб-клиент и сервер прошивки (`www/src/util/Pbkdf2.ts`, `fw/camd/core/Auth.cpp`).

    Args:
        password: пароль камеры.
        session_id: идентификатор сеанса из `create_session`.
        salt: соль из ответа на вызов, 32 шестнадцатеричных знака.
        iterations: число итераций PBKDF2 из ответа на вызов.
    """
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return hmac.new(key, session_id.encode(), "sha256").hexdigest()


def parse_challenge(response: http.Response) -> tuple[str, int] | None:
    """Соль и итерации из ответа на вызов; None — у камеры вход MD5.

    Ответ проверяется строго: заводская прошивка на незнакомый адрес может ответить чем угодно,
    и только полный правильный ответ означает вход PBKDF2.
    """
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("algorithm") != CHALLENGE_ALGORITHM:
        return None
    salt, iterations = data.get("salt"), data.get("iterations")
    if not isinstance(salt, str) or not _SALT.fullmatch(salt):
        return None
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        return None
    return salt, iterations


def error_code(response: http.Response) -> int | None:
    """Внутренний код ошибки камеры из ответа; None — ответ без кода."""
    try:
        code = response.json().get("errorCode")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, int) else None


class RuverClient:
    """Сеанс с API одной камеры. Используется только как контекстный менеджер.

    Вход — при входе в контекст, выход — при выходе из него, в том числе при сбое. Сеанс,
    открытый перед отказом в пароле, тоже закрывается: иначе он занял бы один из 3 сеансов.
    """

    def __init__(
        self,
        address: str,
        port: int,
        username: str,
        password: str,
        *,
        api: ApiMap | None = None,
        allow_writes: bool = False,
        allow_destructive: bool = False,
        timeout: float = 5.0,
    ) -> None:
        """Подготовить клиента; в сеть он пойдёт только при входе в контекст.

        Args:
            address: адрес камеры из списка стенда.
            port: порт HTTP.
            username: логин.
            password: пароль; в запрос попадает только как MD5 или HMAC с сеансом.
            api: карта API; по умолчанию — из файла программы.
            allow_writes: галочка сценария «изменяющие».
            allow_destructive: галочка сценария «разрушающие».
            timeout: тайм-аут одного запроса, с.
        """
        self.base = f"http://{address}:{port}"
        self.username = username
        self.password = password
        self.api = api or load_map()
        self.allow_writes = allow_writes
        self.allow_destructive = allow_destructive
        self.timeout = timeout
        self.session_id = ""
        self._relogin_used = False
        self._login_refused = False
        # соль и итерации входа PBKDF2, None — вход MD5; узнаётся один раз на клиента
        self._challenge: tuple[str, int] | None = None
        self._challenge_known = False
        self.login_method = ""  # «PBKDF2» или «MD5» после входа — для доказательства проверки

    def __enter__(self) -> "RuverClient":
        try:
            self._login()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", SESSION_HEADER: self.session_id}

    def _login(self) -> None:
        """Открыть сеанс и войти: ровно одна попытка.

        Raises:
            NoFreeSession: заняты все сеансы.
            LoginRejected: камера не приняла логин или пароль.
            LoginFailed: неожиданный ответ или блокировка попыток.
            http.TransportError: камера не ответила.
        """
        created = http.request("GET", self.base + CREATE_SESSION, timeout=self.timeout)
        if created.status_code == 409:
            raise NoFreeSession("нет свободного сеанса: на камере заняты все сеансы")
        if created.status_code != 200:
            raise LoginFailed(f"сеанс не открыт: код {created.status_code}")
        try:
            self.session_id = str(created.json()["sessionID"])
        except (ValueError, KeyError, TypeError) as e:
            raise LoginFailed("сеанс не открыт: в ответе нет sessionID") from e
        challenge = self._ask_challenge()
        if challenge is None:
            password = password_hash(self.password, self.session_id)
            self.login_method = "MD5"
        else:
            password = challenge_password(self.password, self.session_id, *challenge)
            self.login_method = "PBKDF2"
        body = {"username": self.username, "password": password}
        answer = http.request(
            "POST",
            self.base + LOGIN,
            content=json.dumps(body),
            headers=self._headers(),
            timeout=self.timeout,
        )
        if answer.status_code == 200:
            return
        code = error_code(answer)
        if answer.status_code == 409:
            raise NoFreeSession("нет свободного сеанса: на камере заняты все сеансы")
        # 429 — блокировка сторожа попыток прошивки своей сборки; код 2003 в ней бывает не всегда
        if code == 2003 or answer.status_code == 429:
            self._login_refused = True
            raise LoginFailed("камера заблокировала вход: превышено число попыток")
        if answer.status_code in (401, 404):
            self._login_refused = True
            raise LoginRejected(f"камера не приняла логин или пароль (код {code})")
        raise LoginFailed(f"вход не состоялся: код {answer.status_code}")

    def _ask_challenge(self) -> tuple[str, int] | None:
        """Узнать вход камеры по вызову, один раз на клиента.

        Raises:
            LoginFailed: камера требует больше итераций, чем разумно считать.
            http.TransportError: камера не ответила.
        """
        if not self._challenge_known:
            answer = http.request(
                "POST",
                self.base + CHALLENGE,
                content=json.dumps({"username": self.username}),
                headers=self._headers(),
                timeout=self.timeout,
            )
            self._challenge = parse_challenge(answer)
            self._challenge_known = True
        if self._challenge and self._challenge[1] > MAX_ITERATIONS:
            raise LoginFailed(
                f"вход не начат: камера требует {self._challenge[1]} итераций PBKDF2, "
                f"разумный предел — {MAX_ITERATIONS}"
            )
        return self._challenge

    def close(self) -> None:
        """Закрыть сеанс; сбой выхода не мешает итогу проверки. Повторный вызов безвреден."""
        session, self.session_id = self.session_id, ""
        if not session:
            return
        headers = {"Content-Type": "application/json", SESSION_HEADER: session}
        # камера перезагружается или пропала: сеанс закрылся вместе с ней
        with contextlib.suppress(http.TransportError):
            http.request("POST", self.base + LOGOUT, headers=headers, timeout=self.timeout)

    def expect_restart(self) -> None:
        """Разрешить ещё один повторный вход: служба API камеры перезапускается ожидаемо.

        Пароль уже подтверждён, а сеанс закрывает сама камера. После отказа в пароле или
        блокировки вход не разрешается: он расходует попытки.
        """
        self._relogin_used = self._login_refused

    def _check_allowed(self, op: Operation) -> None:
        if self.api.get(op.method, op.path) != op:
            raise CallRefused(f"{op.method} {op.path}: операции нет в карте API")
        if op.kind in (TABOO, NOT_CALLED):
            raise CallRefused(f"{op.method} {op.path}: вид «{op.kind}» не вызывается")
        if op.kind in (WRITE, RETURN_ONLY) and not self.allow_writes:
            raise CallRefused(f"{op.method} {op.path}: изменяющие проверки не разрешены")
        if op.kind == DESTRUCTIVE and not self.allow_destructive:
            raise CallRefused(f"{op.method} {op.path}: разрушающие проверки не разрешены")

    def call(
        self,
        op: Operation,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> http.Response:
        """Вызвать операцию карты в открытом сеансе.

        Args:
            op: операция из карты; параметры адреса берутся из неё.
            body: тело JSON; по умолчанию — тело из карты.
            query: параметры адреса вместо параметров из карты.

        Returns:
            Ответ камеры с любым кодом: что он значит, решает проверка.

        Raises:
            CallRefused: операции нет в карте или сценарий её не разрешил; в сеть не ушло.
            TabooViolation: запрос запрещён табу; в сеть не ушёл.
            LoginRejected, LoginFailed, NoFreeSession: повторный вход не удался.
            http.TransportError: камера не ответила.
        """
        self._check_allowed(op)
        parameters = op.query if query is None else query
        url = self.base + op.path + (f"?{urlencode(parameters)}" if parameters else "")
        payload = body if body is not None else op.body
        content = json.dumps(payload) if payload is not None else b""

        def send() -> http.Response:
            return http.request(
                op.method, url, content=content, headers=self._headers(), timeout=self.timeout
            )

        response = send()
        if (
            response.status_code == 401
            and not self._relogin_used
            and error_code(response) in SESSION_GONE_CODES
        ):
            self._relogin_used = True
            self.session_id = ""
            self._login()
            response = send()
        return response
