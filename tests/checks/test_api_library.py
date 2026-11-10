"""Библиотека карт и пар (api-discovery, T14): папки модели и прошивки, атомарная запись,
чтение с проверкой, старшинство источников пар, сверка по библиотеке в API-MAP-1."""

import json
import os

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.api.discovery import CHECK_ID
from qa_tool.checks.api.library import (
    CONFIRMED,
    LibraryPairs,
    firmware_dir,
    load_library,
    model_dir,
    part_name,
    resolve,
    save,
)
from qa_tool.checks.core.connect import PASSPORT_KEY
from qa_tool.core.api_map import LIBRARY_KEY, NativeMap, Pair, PairTable, Scale, dumps_pairs
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from tests.mock_camera import MockCamera, Passport
from tests.mock_native import dahua

load_all()
SECRET = "test-secret-10"  # выдуманный: настоящие пароли стенда в репозиторий не попадают
BRIGHT = "VideoColor[0][0].Brightness"


def found(n=1, native=BRIGHT, source="найдено автоматически, прогон QA-1", state="найдена"):
    return Pair(
        n, "Imaging/Brightness", native, Scale("линейная", 1.0, 0.0), source=source, state=state
    )


def write(folder, name, pairs, firmware="2.800"):
    folder.mkdir(parents=True, exist_ok=True)
    table = PairTable("Dahua", "IPC-1", firmware, pairs)
    (folder / name).write_text(dumps_pairs(table), encoding="utf-8")


def test_folder_names_are_safe_and_distinct(tmp_path):
    assert part_name("IPC-1") != part_name("IPC-2")
    assert part_name("A/B") != part_name("A_B")  # одинаковая очистка — разный хеш
    for hostile in ("../..", "CON", "a\\b", ""):
        name = part_name(hostile)
        assert "/" not in name and "\\" not in name and not name.startswith(".")
    folder = firmware_dir(tmp_path, "dahua", "../../etc", "x")
    assert folder.resolve().is_relative_to(tmp_path.resolve())


def test_save_and_load_past_pairs(tmp_path):
    native = NativeMap(
        brand="dahua", collector="CGI", model="IPC-1", firmware="2.800", run="QA-1", place=2
    )
    problems = save(
        tmp_path,
        native,
        PairTable("Dahua", "IPC-1", "2.800", [found(), found(2, state="кандидат")]),
    )
    assert problems == []
    folder = firmware_dir(tmp_path, "dahua", "IPC-1", "2.800")
    assert sorted(p.name for p in folder.iterdir()) == ["map-QA-1-2.json", "pairs-QA-1-2.json"]
    library = load_library(tmp_path, "dahua", "IPC-1", "2.800")
    assert [p.n for p in library.past] == [1]  # кандидат в сверку не идёт


def test_latest_run_first_and_confirmed(tmp_path):
    folder = firmware_dir(tmp_path, "dahua", "IPC-1", "2.800")
    write(folder, "pairs-QA-20261108-001-1.json", [found(source="старый")])
    write(folder, "pairs-QA-20261109-002-1.json", [found(source="новый")])
    write(
        folder,
        CONFIRMED,
        [found(3, source="подтверждено вручную: ОТК", state="подтверждена вручную")],
    )
    library = load_library(tmp_path, "dahua", "IPC-1", "2.800")
    assert [p.source for p in library.past] == ["новый", "старый"]
    assert [p.n for p in library.confirmed] == [3]


def test_other_firmware_pairs_are_marked(tmp_path):
    write(
        firmware_dir(tmp_path, "dahua", "IPC-1", "2.700"),
        "pairs-QA-1-1.json",
        [found()],
        firmware="2.700",
    )
    library = load_library(tmp_path, "dahua", "IPC-1", "2.800")
    assert library.past == [] and library.other_firmware[0].source.endswith(
        "(найдено на прошивке 2.700)"
    )


def test_hostile_files_are_skipped_with_reason(tmp_path):
    root = tmp_path / "lib"
    folder = firmware_dir(root, "dahua", "IPC-1", "2.800")
    write(folder, "pairs-QA-3-1.json", [found(), found(99)])
    (folder / "pairs-QA-4-1.json").write_bytes(b" " * (3 << 20))
    (folder / "pairs-QA-5-1.json").write_text('{"формат": "чужой"}', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text(dumps_pairs(PairTable("x", "y", "z", [found(4)])), encoding="utf-8")
    try:
        os.symlink(outside, folder / "pairs-QA-6-1.json")
    except OSError:
        pytest.skip("ссылки не создаются на этой системе")
    library = load_library(root, "dahua", "IPC-1", "2.800")
    assert [p.n for p in library.past] == [1]
    text = " ".join(library.problems)
    assert "n=99" in text and "больше" in text and "не прочитан" in text and "ссылка" in text


def test_resolve_precedence():
    manual = [
        found(1, "A", "вручную", "подтверждена вручную"),
        found(2, "B", "вручную", "подтверждена вручную"),
    ]
    library = LibraryPairs(
        confirmed=[found(3, "C", "ОТК", "подтверждена вручную"), found(1, "X", "ОТК")],
        past=[found(4, "D", "прогон QA-0"), found(3, "Y", "прогон QA-0")],
        other_firmware=[found(5, "E", "прогон QA-0 (найдено на прошивке 1)")],
    )
    this_run = [
        found(1, "A", "найдено автоматически, прогон QA-9"),
        found(2, "Z", "найдено автоматически, прогон QA-9"),
        found(6, "F", "найдено автоматически, прогон QA-9"),
    ]
    pairs = {p.n: p for p in resolve(this_run, manual, library)}
    assert pairs[1].native == "A" and pairs[1].source.endswith("подтверждено вручную")
    assert pairs[2].native == "B" and "нашёл иначе: Z" in pairs[2].source
    assert pairs[3].native == "C" and pairs[4].native == "D" and pairs[5].native == "E"
    assert pairs[6].native == "F"


def test_check_compares_by_library_on_next_run(tmp_path):
    api = dahua(password=SECRET)
    camera = MockCamera(
        Passport("Dahua", "IPC-1", "2.800", "SN"), password=SECRET, native=api
    ).start()
    try:
        place = Camera(
            slot=1, address="127.0.0.1", port=camera.port, username="admin", vendor="dahua"
        )
        passport = {"manufacturer": "Dahua", "model": "IPC-1", "firmware": "2.800", "serial": "SN"}
        extra = {PASSPORT_KEY: passport, LIBRARY_KEY: str(tmp_path), "run_id": "QA-1"}
        ctx = CheckContext(place, SECRET, Place.COLOR_BOX, extra=extra)
        first = run_check(REGISTRY.get(CHECK_ID), ctx)
        assert [c.check_id for c in first.children] == ["API-MAP-1", "API-MATCH-0"]
        folder = firmware_dir(tmp_path, "dahua", "IPC-1", "2.800")
        assert (folder / "map-QA-1-1.json").is_file()
        write(
            folder,
            CONFIRMED,
            [found(1, BRIGHT, "подтверждено вручную: ОТК", "подтверждена вручную")],
        )
        second = run_check(REGISTRY.get(CHECK_ID), ctx)
        rows = {c.check_id: c.result for c in second.children}
        assert rows["API-MATCH-1"].status is Status.PASSED
        assert json.loads((folder / "pairs-QA-1-1.json").read_text(encoding="utf-8"))["пары"] == []
    finally:
        camera.stop()
    assert model_dir(tmp_path, "dahua", "IPC-1").is_dir()
