"""Сборщик-заготовка Tiandy: родной API по HTTP не найден (решение директора 09.11)."""

from qa_tool.drivers.camera.stub_collector import StubCollector

TIANDY = StubCollector(
    "tiandy",
    "заготовка",
    frozenset({"tiandy"}),
    "у Tiandy родной API по HTTP не найден: веб — через плагин браузера, протокол — свой SDK",
)
