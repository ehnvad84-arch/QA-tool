"""Ступени битрейта (codec-quality, T5): общая сетка класса, общий диапазон кодеков, 1/16."""

from qa_tool.checks.codec.ladder import Range, grid, grid_class, ladder
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place, Threshold


def ctx(**overrides) -> CheckContext:
    limits = {k.replace("__", "."): Threshold(v, "", "тест") for k, v in overrides.items()}
    return CheckContext(
        Camera(slot=1, address="10.0.0.1", username="admin"), "", Place.COLOR_BOX, limits
    )


def test_classes_and_start_grid_for_1080p():
    assert [grid_class(h) for h in (480, 720, 1080, 1440, 2160)] == [
        "sd",
        "hd",
        "fhd",
        "qhd",
        "uhd",
    ]
    assert grid(ctx(), 1080) == [8192, 4096, 2048, 1024, 512]


def test_two_cameras_of_one_resolution_get_the_same_steps():
    small = ladder(ctx(), 1080, Range(32, 8192), Range(32, 8192))
    big = ladder(ctx(), 1080, Range(64, 16384), Range(64, 16384))
    assert small.steps_a == [8192, 4096, 2048, 1024, 512]
    assert big.steps_a == [8192, 4096, 2048, 1024]  # 512 ниже 1/16 от 16384
    assert set(big.steps_a) <= set(small.steps_a) and small.common


def test_common_range_of_two_codecs():
    found = ladder(ctx(), 1080, Range(256, 8192), Range(128, 4096))
    assert found.steps_a == found.steps_b == [4096, 2048, 1024, 512] and found.common


def test_codecs_without_overlap_get_own_steps():
    found = ladder(ctx(), 1080, Range(4096, 16384), Range(64, 2048))
    assert not found.common and "не сравнить" in found.note
    assert found.steps_a == [8192, 4096] and found.steps_b == [2048, 1024, 512]


def test_narrow_range_is_skipped():
    found = ladder(ctx(), 1080, Range(3000, 5000))
    assert found.steps_a == [] and "не покрывает сетку" in found.note


def test_grid_from_configuration():
    custom = ctx(codec__fhd__grid_top=6000, codec__grid_steps=3, codec__grid_ratio=3)
    assert grid(custom, 1080) == [6000, 2000, 667]
