"""Выбор сборщика по паспорту и месту стенда; заготовки Tiandy и Beward (api-discovery, T9)."""

import pytest

from qa_tool.core.api_map import NativeMap
from qa_tool.core.models import Camera
from qa_tool.drivers.camera.collectors import all_collectors
from qa_tool.drivers.camera.http import HttpLogin
from qa_tool.drivers.camera.native import NativeReader, choose_collector
from qa_tool.gui.camera_dialog import VENDORS


@pytest.mark.parametrize(
    ("maker", "vendor", "brand"),
    [
        ("HIKVISION", "generic", "hikvision"),
        ("Dahua", "generic", "dahua"),
        ("Ruware", "generic", "ruver"),
        ("TIANDY", "generic", "tiandy"),
        ("Beward", "generic", "beward"),
        ("", "tiandy", "tiandy"),
        ("", "beward", "beward"),
        ("Axis", "axis", None),
    ],
)
def test_every_stand_brand_has_a_collector(maker, vendor, brand):
    camera = Camera(slot=1, address="127.0.0.1", username="admin", vendor=vendor)
    choice = choose_collector(all_collectors(), {"manufacturer": maker}, camera)
    assert (choice.collector.brand if choice.collector else None) == brand


def test_vendor_list_offers_every_collector_brand():
    brands = {c.brand for c in all_collectors()}
    assert brands <= set(VENDORS)


@pytest.mark.parametrize("brand", ["tiandy", "beward"])
def test_stub_collector_sends_nothing(brand, monkeypatch):
    from qa_tool.drivers.camera import http

    def forbidden(*args, **kwargs):
        raise AssertionError("заготовка не должна ходить к камере")

    monkeypatch.setattr(http, "request", forbidden)
    collector = next(c for c in all_collectors() if c.brand == brand)
    camera = Camera(slot=1, address="127.0.0.1", username="admin", vendor=brand)
    result = NativeReader(camera, collector, HttpLogin("admin", "test-secret-7")).read(
        NativeMap(brand=brand, collector=collector.name)
    )
    assert result.sections == [] and result.errors == [] and not result.cut
    assert collector.why
