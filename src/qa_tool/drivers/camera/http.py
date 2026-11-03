"""Единственная точка, через которую программа шлёт камерам HTTP-запросы (AD-7).

Любой драйвер, адаптер производителя и веб-обход ходят к камере только через `request()`:
здесь стоит табу — запрещённый запрос не уходит в сеть. Архитектурный тест
tests/test_architecture.py не даёт импортировать httpx, requests и сокеты в обход этого модуля.
"""

import httpx

from qa_tool.core.taboo import check_http, check_onvif, check_web_request

TransportError = httpx.TransportError  # драйверы ловят сетевые сбои, не импортируя httpx


def _is_soap(headers: dict[str, str], text: str) -> bool:
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    return "soap" in content_type.lower() or "Envelope" in text[:500]


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

    Raises:
        TabooViolation: запрос меняет сеть, учётные записи или делает сброс —
            в сеть он не отправлен.
        TransportError: камера не ответила.
    """
    headers = headers or {}
    text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
    if _is_soap(headers, text):
        check_onvif(text)  # в SOAP-конверте всегда есть Username/Password — словами не судим
    else:
        check_http(method, url)
        check_web_request(method, url, text)
    return httpx.request(method, url, content=content, headers=headers, timeout=timeout)
