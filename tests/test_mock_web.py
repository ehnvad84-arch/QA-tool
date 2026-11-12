"""Свидетель mock должен видеть запись, иначе тесты перехвата ничего не доказывают."""

import httpx

from tests.mock_web import MockWebCamera


def test_witness_counts_what_arrived():
    with MockWebCamera({"/": "<h1>камера</h1>"}) as camera:
        assert httpx.get(camera.url + "/").status_code == 200
        assert httpx.post(camera.url + "/api/set", json={"a": 1}).status_code == 200
        assert httpx.get(camera.url + "/none").status_code == 404
    assert camera.arrived == [("GET", "/"), ("POST", "/api/set"), ("GET", "/none")]
    assert camera.writes() == [("POST", "/api/set")]
