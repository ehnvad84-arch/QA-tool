"""Каталог настроек ONVIF и ручные таблицы пар (api-discovery, T11)."""

import json

import pytest

from qa_tool.checks.api import catalog
from qa_tool.checks.api.catalog import BY_N, CATALOG, ENCODER, IMAGE, manual_pairs
from qa_tool.checks.image.settings import BY_KEY
from qa_tool.core.api_map import NativeMap
from qa_tool.core.models import Camera
from qa_tool.drivers.camera import http
from qa_tool.drivers.camera.collectors import all_collectors
from qa_tool.drivers.camera.hikvision.collector import HikvisionCollector
from qa_tool.drivers.camera.http import HttpLogin
from qa_tool.drivers.camera.imaging import PROTECTED
from qa_tool.drivers.camera.native import NativeReader
from tests.mock_native import hikvision

ENCODER_FIELDS = {"kbps", "encoding"}


def test_numbers_are_unique_and_without_gaps():
    numbers = [e.n for e in CATALOG]
    assert numbers == list(range(1, len(CATALOG) + 1))


def test_search_writes_only_unprotected_known_settings():
    for entry in CATALOG:
        if entry.cls == IMAGE and entry.path[0] in PROTECTED:
            assert not entry.writes, entry
        if entry.writes:
            known = BY_KEY if entry.cls == IMAGE else ENCODER_FIELDS
            assert entry.setting in known, entry
    assert [e.n for e in CATALOG if e.cls == ENCODER and e.writes] == [15, 16]


def test_mode_entries_point_to_their_levels():
    for entry in CATALOG:
        for level in entry.mode_of:
            assert BY_N[level].path[0] == entry.path[0] and BY_N[level].writes


def test_onvif_names():
    assert BY_N[1].onvif == "Imaging/Brightness"
    assert BY_N[15].onvif == "VideoEncoder/RateControl/BitrateLimit"


@pytest.mark.parametrize("brand", [c.brand for c in all_collectors()])
def test_every_brand_has_a_readable_manual_table(brand):
    table = manual_pairs(brand)
    assert table.problem == ""


def test_hikvision_pairs_of_0911_match_the_virtual_camera_map(monkeypatch):
    pairs = manual_pairs("hikvision").pairs
    assert {p.n: p.scale.kind for p in pairs} == {12: "перечисление", 13: "по диапазонам"}
    api = hikvision(password="test-secret-8")
    monkeypatch.setattr(http, "_TRANSPORT", api.transport())
    camera = Camera(slot=1, address="127.0.0.1", username="admin", port=9100)
    native = NativeReader(camera, HikvisionCollector(), HttpLogin("admin", "test-secret-8")).read(
        NativeMap(brand="hikvision", collector="ISAPI")
    )
    assert all(p.native in native.params() for p in pairs)


def write_table(tmp_path, monkeypatch, pairs, name="hikvision"):
    monkeypatch.setattr(catalog, "PAIRS_DIR", tmp_path)
    data = {
        "формат": "qa-tool/api-pairs",
        "версия": 1,
        "камера": {"производитель": "Hikvision", "модель": "", "прошивка": ""},
        "пары": pairs,
    }
    (tmp_path / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def pair(n, **extra):
    return {
        "n": n,
        "onvif": "x",
        "родной": "Image/channels/1/Color/brightnessLevel",
        "шкала": {"вид": "линейная", "a": 1, "b": 0},
        "состояние": "подтверждена вручную",
        "источник": "тест",
        **extra,
    }


def test_pairs_are_filtered_by_model_and_firmware(tmp_path, monkeypatch):
    write_table(
        tmp_path,
        monkeypatch,
        [pair(1), pair(2, модели=["DS-A"]), pair(3, прошивки=["V5.7"]), pair(99)],
    )
    table = manual_pairs("hikvision", model="DS-B", firmware="V5.7")
    assert [p.n for p in table.pairs] == [1, 3]
    assert "n=99" in table.problem


def test_broken_or_foreign_table_gives_problem_not_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "PAIRS_DIR", tmp_path)
    (tmp_path / "hikvision.json").write_text('{"формат": "чужой"}', encoding="utf-8")
    table = manual_pairs("hikvision")
    assert table.pairs == () and "не прочитана" in table.problem
    assert manual_pairs("axis").problem == ""  # таблицы нет — не ошибка
    assert "не годится" in manual_pairs("../hikvision").problem
