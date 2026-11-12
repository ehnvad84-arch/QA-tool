"""Табу держится в коде (AD-7): к камере ходят только через drivers/camera/http.py
(HTTP), drivers/camera/browser.py (браузер веб-обхода), drivers/camera/probe.py, discovery.py (сокеты только на чтение) и rtsp.py
(поток: методы проверяет core/taboo.check_rtsp); taboo_proxy.py принимает запросы onvif-tt
на 127.0.0.1 и пересылает их через http.py.

Импорты разбираются через ast: обход записью «from urllib import request» или
«import os, socket» тоже ловится."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "qa_tool"
NETWORK_MODULES = {
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
    "socket",
    "http.client",
    "urllib.request",
    "zeep",
    "onvif",
    "websocket",
    "websockets",
    "pycurl",
    "ftplib",
    "telnetlib",
    "smtplib",
    "http.server",
    "socketserver",
    "asyncio",
    "playwright",
}
ALLOWED = {
    "drivers/camera/http.py": {"httpx"},
    "drivers/camera/probe.py": {"socket"},
    "drivers/camera/discovery.py": {"socket"},
    "drivers/camera/rtsp.py": {"socket"},
    # посредник табу слушает только 127.0.0.1, к камере пересылает через http.py
    "drivers/camera/taboo_proxy.py": {"http.server"},
    # браузер веб-обхода: перехват и проверки табу стоят внутри этого модуля
    "drivers/camera/browser.py": {"playwright"},
}
GUARDED = "qa_tool.drivers.camera.http"


def network_imports(source: str) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            # «from qa_tool.drivers.camera.http import httpx» — тот же httpx в обход request()
            if node.module == GUARDED and any(a.name == "httpx" for a in node.names):
                found.add("httpx")
        elif isinstance(node, ast.Attribute) and node.attr == "httpx":
            found.add("httpx")  # «http.httpx.post(...)»
            continue
        else:
            continue
        for name in names:
            for module in NETWORK_MODULES:
                if name == module or name.startswith(module + "."):
                    found.add(module)
    return found


def test_network_only_through_guarded_modules():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        extra = network_imports(path.read_text(encoding="utf-8")) - ALLOWED.get(rel, set())
        if extra:
            offenders.append(f"{rel}: {sorted(extra)}")
    assert not offenders, "сеть в обход табу: " + "; ".join(offenders)


def test_import_tricks_are_caught():
    tricks = [
        "from urllib import request",
        "from http import client",
        "import os, socket",
        "from onvif import ONVIFCamera",
        "from qa_tool.drivers.camera.http import httpx",
        "from qa_tool.drivers.camera import http\nhttp.httpx.post('x')",
        "import zeep.transports",
        "from playwright.sync_api import sync_playwright",
    ]
    for trick in tricks:
        assert network_imports(trick), trick
