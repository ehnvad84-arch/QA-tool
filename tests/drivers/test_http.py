"""Сетевая точка драйверов: то, что не относится к табу."""

import httpx

from qa_tool.drivers.camera import http


def test_tls_context_is_built_once(monkeypatch):
    """Сертификаты читаются один раз на процесс: на Windows это дороже самого запроса."""
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append(kwargs["verify"])
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "request", fake_request)
    http.request("GET", "http://192.0.2.1/api/info")
    http.request("GET", "http://192.0.2.1/api/info")
    assert seen[0] is seen[1]
