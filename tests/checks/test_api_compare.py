"""Сверка родного API с ONVIF (api-discovery, T12): шкалы, допуски, диапазоны, «край» битрейта,
пропуски по карте и случаи живых камер 09.11."""

import pytest

from qa_tool.checks.api.compare import NO_PAIRS, OnvifSide, Tolerances, compare_pairs, number
from qa_tool.core.api_map import NativeMap, Pair, Scale, Section, allowed_range, allowed_values
from qa_tool.core.checks import Severity, Status

TOL = Tolerances(value_pct=1.0, range_pct=1.0)
IMAGE = "Image/channels/1"


def camera(**params) -> NativeMap:
    """Карта с разделом картинки; значение — (тип, допустимые, значение[, изменчивый])."""
    native = NativeMap(brand="hikvision", collector="ISAPI")
    section = Section(IMAGE, "картинка", "GET /ISAPI/Image/channels/1")
    for key, spec in params.items():
        type_, allowed, value, *rest = spec
        section.add_param(f"{IMAGE}/{key}", type_, allowed, value, volatile=bool(rest))
    native.sections.append(section)
    return native


def pair(n, native, kind="линейная", **scale) -> Pair:
    values = scale.pop("values", None)
    return Pair(
        n,
        "x",
        f"{IMAGE}/{native}",
        Scale(kind, values=values, **scale),
        source="подтверждено вручную: тест",
        state="подтверждена вручную",
    )


def rows(native, onvif, pairs):
    return {r.check_id: r for r in compare_pairs(native, onvif, pairs, TOL, "ISAPI")}


def test_exposure_mismatch_like_111():
    native = camera(ExposureType=("перечисление", allowed_values(["auto", "manual"]), "manual"))
    found = rows(
        native,
        {12: OnvifSide("AUTO")},
        [pair(12, "ExposureType", "перечисление", values={"AUTO": "auto", "MANUAL": "manual"})],
    )
    row = found["API-MATCH-12"]
    assert row.result.status is Status.FAILED and row.severity is Severity.MAJOR
    assert row.result.actual == (
        "ONVIF Imaging/Exposure/Mode = AUTO, ISAPI Image/channels/1/ExposureType = manual"
    )
    assert "API-RANGE-12" not in found
    assert "источник: подтверждено вручную: тест" in row.result.evidence[0].content


def test_enum_match_and_unknown_value():
    native = camera(ExposureType=("перечисление", None, "Auto"))
    exposure = pair(12, "ExposureType", "перечисление", values={"AUTO": "auto"})
    assert (
        rows(native, {12: OnvifSide("auto")}, [exposure])["API-MATCH-12"].result.status
        is Status.PASSED
    )
    unknown = rows(native, {12: OnvifSide("MANUAL")}, [exposure])["API-MATCH-12"].result
    assert unknown.status is Status.SKIPPED and "не встречалось при поиске" in unknown.reason


def test_max_gain_by_ranges():
    native = camera(GainLevel=("целое", allowed_range(0, 100), "40"))
    gain = pair(13, "GainLevel", "по диапазонам")
    ok = rows(native, {13: OnvifSide("102", 0, 255)}, [gain])
    assert ok["API-MATCH-13"].result.status is Status.PASSED
    assert "API-RANGE-13" not in ok  # шкала построена из самих диапазонов
    bad = rows(native, {13: OnvifSide("255", 0, 255)}, [gain])["API-MATCH-13"].result
    assert bad.status is Status.FAILED and "→ 100" in bad.actual
    no_range = rows(native, {13: OnvifSide("102")}, [gain])["API-MATCH-13"].result
    assert no_range.status is Status.SKIPPED and "годного диапазона нет" in no_range.reason


@pytest.mark.parametrize(
    ("onvif", "native_value", "status"),
    [("128", "50", Status.PASSED), ("128", "51", Status.PASSED), ("128", "70", Status.FAILED)],
)
def test_linear_brightness_within_tolerance(onvif, native_value, status):
    native = camera(brightnessLevel=("целое", allowed_range(0, 100), native_value))
    bright = pair(1, "brightnessLevel", a=100 / 255, b=0.0)
    row = rows(native, {1: OnvifSide(onvif, 0, 255)}, [bright])
    assert row["API-MATCH-1"].result.status is status
    assert row["API-RANGE-1"].result.status is Status.PASSED
    assert "0…255 ≈ 0…100" in row["API-RANGE-1"].result.actual


def test_range_mismatch_is_minor():
    native = camera(brightnessLevel=("целое", allowed_range(0, 50), "25"))
    found = rows(
        native, {1: OnvifSide("64", 0, 255)}, [pair(1, "brightnessLevel", a=100 / 255, b=0)]
    )
    row = found["API-RANGE-1"]
    assert row.result.status is Status.FAILED and row.severity is Severity.MINOR
    assert row.result.actual == "ONVIF 0…255 ≈ 0…100, ISAPI 0…50"


def test_bitrate_edge_like_108_is_major():
    native = camera(BitRate=("целое", allowed_range(512, 5632), "1792"))
    side = OnvifSide("1792", 512, 5632, held=1792, held_note="при H.264")
    row = rows(native, {15: side}, [pair(15, "BitRate", a=1, b=0)])["API-RANGE-15"]
    assert row.result.status is Status.FAILED and row.severity is Severity.MAJOR
    assert row.result.actual == (
        "объявлено 512…5632 и в ONVIF, и в ISAPI, а при H.264 камера держит не больше 1792"
    )


def test_garbage_ranges_fail_as_declared_badly():
    native = camera(brightnessLevel=("целое", allowed_range(100, 0), "50"))
    row = rows(native, {1: OnvifSide("50", 0, 100)}, [pair(1, "brightnessLevel", a=1, b=0)])
    assert "негодный диапазон" in row["API-RANGE-1"].result.actual
    native = camera(brightnessLevel=("целое", allowed_range(0, 100), "50"))
    row = rows(native, {1: OnvifSide("50", 100, 0)}, [pair(1, "brightnessLevel", a=1, b=0)])
    assert row["API-RANGE-1"].result.status is Status.FAILED


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({}, "нет в карте камеры"),
        ({"brightnessLevel": ("строка", None, "high")}, "не число"),
        ({"brightnessLevel": ("целое", None, "50", True)}, "меняется само"),
    ],
)
def test_pair_not_applicable_to_this_map(params, reason):
    native = camera(**params)
    row = rows(native, {1: OnvifSide("50")}, [pair(1, "brightnessLevel", a=1, b=0)])["API-MATCH-1"]
    assert row.result.status is Status.SKIPPED and reason in row.result.reason
    assert row.severity is None


def test_missing_onvif_value_is_skipped():
    native = camera(brightnessLevel=("целое", None, "50"))
    row = rows(native, {}, [pair(1, "brightnessLevel", a=1, b=0)])["API-MATCH-1"]
    assert row.result.status is Status.SKIPPED and "ONVIF не отдал" in row.result.reason


def test_no_usable_pairs_gives_one_skipped_row():
    native = camera(brightnessLevel=("целое", None, "50"))
    candidate = Pair(1, "x", f"{IMAGE}/brightnessLevel", Scale("линейная", 1, 0), state="кандидат")
    points = pair(2, "brightnessLevel", "по точкам")
    found = compare_pairs(native, {}, [candidate, points], TOL, "ISAPI")
    assert [r.check_id for r in found] == ["API-MATCH-0"]
    assert found[0].result.reason == NO_PAIRS
    assert compare_pairs(native, {}, [], TOL, "ISAPI")[0].check_id == "API-MATCH-0"


def test_number_uses_comma():
    assert number(0.392) == "0,392" and number(100.0) == "100"
