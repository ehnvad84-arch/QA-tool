"""Единственная точка, через которую программа шлёт камерам HTTP-запросы (AD-7).

Любой драйвер и адаптер производителя ходят к камере только через `request()`: здесь стоит
табу — запрещённый запрос не уходит в сеть. У браузера веб-обхода свой выход под теми же
проверками — `drivers/camera/browser.py`. Архитектурный тест tests/test_architecture.py
не даёт импортировать httpx, requests и сокеты в обход этих модулей.
"""

import base64
import functools
import logging
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from qa_tool.core.taboo import MulticastGuard, check_http, check_onvif, check_web_request
from qa_tool.drivers.camera.digest import (
    LoginUnsupported,
    check_challenge,
    digest_header,
    parse_challenge,
)

TransportError = httpx.TransportError  # драйверы ловят сетевые сбои, не импортируя httpx
Response = httpx.Response
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)  # соединения не было: запрос не ушёл

# onvif-tt при загрузке включает журнал INFO на весь процесс, и httpx пишет в консоль строку на
# каждый запрос к камере. Обмены и так сохраняются в доказательствах (с вырезанными секретами).
logging.getLogger("httpx").setLevel(logging.WARNING)


@functools.cache
def _tls_context() -> ssl.SSLContext:
    """Один контекст TLS на процесс.

    Без него httpx заново читает сертификаты на каждый запрос: на Windows это около 0,15 с,
    больше времени самого запроса к камере.
    """
    return httpx.create_ssl_context()


# мультикаст в конфигурациях медиа — сеть (AD-7): помним его из ответов камер этого процесса
_MULTICAST = MulticastGuard()

_ENVELOPE = re.compile(r"<\s*(?:[\w.-]+:)?Envelope\b")
_TRANSPORT: httpx.BaseTransport | None = None  # тесты подменяют сеть; в работе — None
UNAUTHORIZED = 401


class LoginRefused(RuntimeError):
    """Камера отвергла логин и пароль родного API — повтора на этом прогоне нет."""


class ResponseTooLarge(RuntimeError):
    """Ответ камеры больше предела `max_bytes` — дочитывать не стали."""


@dataclass
class HttpLogin:
    """Вход HTTP одной камеры (Digest MD5, Basic только по HTTPS) — одна попытка на прогон.

    Запрос входа камеры запоминается, дальше растёт счётчик `nc`, поэтому лишнего 401 на каждый
    запрос нет. Отказ (401 на подписанный запрос) — `refused`, и следующие запросы с этим входом
    не уходят. Признак отказа на весь прогон камеры живёт выше, в проверках: `already_refused` —
    причина, если пароль уже отверг кто-то другой (RTSP, веб-обход), `on_refused` — запомнить свой
    отказ для остальных (`checks/rtsp_refusal.py`). Не потокобезопасен: один вход — одна нить.
    """

    username: str
    password: str = field(repr=False)
    already_refused: str = ""
    on_refused: Callable[[str], None] | None = field(default=None, repr=False)
    refused: str = ""
    challenge: dict[str, str] | None = field(default=None, repr=False)
    count: int = 0

    def header(self, method: str, url: str) -> str:
        """Заголовок Authorization для запроса по запомненному запросу входа."""
        assert self.challenge is not None
        parts = urlsplit(url)
        uri = parts.path + (f"?{parts.query}" if parts.query else "")
        self.count += 1
        return digest_header(self.username, self.password, self.challenge, method, uri, self.count)

    def refuse(self, reason: str) -> LoginRefused:
        """Запомнить отказ и вернуть исключение для вызывающего."""
        self.refused = reason
        if self.on_refused is not None:
            self.on_refused(reason)
        return LoginRefused(reason)


def is_soap(headers: dict[str, str], text: str) -> bool:
    """SOAP — по Content-Type или по настоящему элементу Envelope, а не по слову в JSON."""
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    return "soap" in content_type.lower() or bool(_ENVELOPE.search(text))


def request(
    method: str,
    url: str,
    *,
    content: bytes | str = b"",
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    login: HttpLogin | None = None,
    max_bytes: int | None = None,
) -> httpx.Response:
    """Отправить HTTP-запрос камере, предварительно проверив табу (AD-7).

    Args:
        method: HTTP-метод.
        url: полный адрес на камере.
        content: тело запроса; SOAP определяется по Content-Type или конверту.
        headers: заголовки.
        timeout: тайм-аут, с.
        login: вход HTTP (родной API камеры); None — без входа, как раньше.
        max_bytes: предел размера ответа, байт; None — без предела.

    Returns:
        Ответ httpx.

    Проверяются всегда путь (`check_http`), операции ONVIF в теле (`check_onvif`) и мультикаст
    в `Set*Configuration` (`MulticastGuard`: он запоминает мультикаст из ответов камеры);
    не-SOAP запрос — ещё и по словам-признакам (`check_web_request`).

    Raises:
        TabooViolation: запрос меняет сеть, учётные записи или делает сброс —
            в сеть он не отправлен.
        TransportError: камера не ответила.
        LoginRefused: камера отвергла вход или его уже отвергли на этом прогоне — без повтора.
        LoginUnsupported: способ входа камеры не поддержан — пароль не отправлен.
        ResponseTooLarge: ответ больше `max_bytes`.
    """
    headers = headers or {}
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    _check(method, url, text, headers)
    host = urlsplit(url).netloc.lower()
    if login is None and max_bytes is None:
        response = httpx.request(
            method, url, content=content, headers=headers, timeout=timeout, verify=_tls_context()
        )
    else:
        response = _with_login(method, url, content, text, headers, timeout, login, max_bytes)
    _MULTICAST.remember(host, response.text)
    return response


def _check(method: str, url: str, text: str, headers: dict[str, str]) -> None:
    """Все проверки табу запроса; повтор с заголовком входа проходит их заново."""
    check_http(method, url)
    check_onvif(text)  # и в «не SOAP»: операцию нельзя протащить без конверта
    _MULTICAST.check(urlsplit(url).netloc.lower(), text)
    if not is_soap(headers, text):
        # в SOAP-конверте всегда есть Username и Password — по словам его не судим
        check_web_request(method, url, text)


def _with_login(
    method: str,
    url: str,
    content: bytes | str,
    text: str,
    headers: dict[str, str],
    timeout: float,
    login: HttpLogin | None,
    max_bytes: int | None,
) -> httpx.Response:
    """Запрос с входом HTTP и пределом размера: одна попытка, `stale` — один повтор."""
    if login is not None and (login.refused or login.already_refused):
        raise LoginRefused(login.refused or login.already_refused)
    signed = dict(headers)
    if login is not None and login.challenge is not None:
        signed["Authorization"] = login.header(method, url)
    response = _send(method, url, content, signed, timeout, max_bytes)
    stale_left = 1  # новый nonce по stale=true — один раз, это не отказ
    while login is not None and response.status_code == UNAUTHORIZED:
        was_signed = "Authorization" in signed
        challenge = _choose(response.headers.get_list("www-authenticate"), url, login)
        if challenge is None:  # остаётся Basic по HTTPS
            if was_signed:
                raise login.refuse("камера не приняла логин и пароль родного API (Basic)")
            token = base64.b64encode(f"{login.username}:{login.password}".encode()).decode()
            signed = {**headers, "Authorization": f"Basic {token}"}
        else:
            if was_signed:
                if challenge.get("stale", "").lower() != "true" or not stale_left:
                    raise login.refuse("камера не приняла логин и пароль родного API")
                stale_left -= 1
            login.challenge, login.count = challenge, 0
            signed = {**headers, "Authorization": login.header(method, url)}
        _check(method, url, text, headers)  # повтор — тот же запрос, табу проверяется заново
        response = _send(method, url, content, signed, timeout, max_bytes)
    return response


def _choose(offered: list[str], url: str, login: HttpLogin) -> dict[str, str] | None:
    """Запрос входа Digest камеры или None, если остаётся Basic по HTTPS.

    Raises:
        LoginUnsupported: ни Digest, ни Basic по HTTPS — пароль не отправлен.
    """
    digests = [parse_challenge(h) for h in offered if h.lower().startswith("digest")]
    if digests:
        check_challenge(digests[0], login.username)
        return digests[0]
    if any(h.lower().startswith("basic") for h in offered):
        if urlsplit(url).scheme != "https":
            raise LoginUnsupported(
                "камера принимает только Basic по HTTP — пароль ушёл бы открытым текстом"
            )
        return None
    raise LoginUnsupported("камера не прислала запрос входа (WWW-Authenticate)")


def _send(
    method: str,
    url: str,
    content: bytes | str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int | None,
) -> httpx.Response:
    """Отправить; ответ читается потоком и обрывается сверх `max_bytes`."""
    with (
        httpx.Client(transport=_TRANSPORT, verify=_tls_context(), timeout=timeout) as client,
        client.stream(method, url, content=content, headers=headers) as response,
    ):
        body = bytearray()
        for chunk in response.iter_bytes():
            body += chunk
            if max_bytes is not None and len(body) > max_bytes:
                raise ResponseTooLarge(f"ответ камеры больше {max_bytes} байт — не дочитан")
        # тело уже распаковано iter_bytes: заголовки сжатия и длины к нему не относятся
        kept = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(
            response.status_code, headers=kept, content=bytes(body), request=response.request
        )
