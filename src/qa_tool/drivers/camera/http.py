"""Единственная точка, через которую программа шлёт камерам HTTP-запросы (AD-7).

Любой драйвер и адаптер производителя ходят к камере только через `request()`: здесь стоит
табу — запрещённый запрос не уходит в сеть. У браузера веб-обхода свой выход под теми же
проверками — `drivers/camera/browser.py`. Архитектурный тест tests/test_architecture.py
не даёт импортировать httpx, requests и сокеты в обход этих модулей.
"""

import functools
import logging
import re
import ssl
from urllib.parse import urlsplit

import httpx

from qa_tool.core.taboo import MulticastGuard, check_http, check_onvif, check_web_request

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
) -> httpx.Response:
    """Отправить HTTP-запрос камере, предварительно проверив табу (AD-7).

    Args:
        method: HTTP-метод.
        url: полный адрес на камере.
        content: тело запроса; SOAP определяется по Content-Type или конверту.
        headers: заголовки.
        timeout: тайм-аут, с.

    Returns:
        Ответ httpx.

    Проверяются всегда путь (`check_http`), операции ONVIF в теле (`check_onvif`) и мультикаст
    в `Set*Configuration` (`MulticastGuard`: он запоминает мультикаст из ответов камеры);
    не-SOAP запрос — ещё и по словам-признакам (`check_web_request`).

    Raises:
        TabooViolation: запрос меняет сеть, учётные записи или делает сброс —
            в сеть он не отправлен.
        TransportError: камера не ответила.
    """
    headers = headers or {}
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    check_http(method, url)
    check_onvif(text)  # и в «не SOAP»: операцию нельзя протащить без конверта
    host = urlsplit(url).netloc.lower()
    _MULTICAST.check(host, text)
    if not is_soap(headers, text):
        # в SOAP-конверте всегда есть Username и Password — по словам его не судим
        check_web_request(method, url, text)
    response = httpx.request(
        method, url, content=content, headers=headers, timeout=timeout, verify=_tls_context()
    )
    _MULTICAST.remember(host, response.text)
    return response
