"""Итоги проверки кодеков (этап 8, T8): случаи таблицы «Итоги и примеры причин» спеки."""

import dataclasses

import pytest

from qa_tool.checks.codec.quality import Quality, Stat
from qa_tool.checks.codec.rules import (
    NO_VMAF,
    Curve,
    Point,
    choose_reference,
    rate,
    scene_problem,
    verdict,
    vmaf_class,
)
from qa_tool.core.checks import CheckContext, Severity, Status
from qa_tool.core.models import Camera, Place, Threshold


def ctx(**thresholds: float) -> CheckContext:
    limits = {f"codec.{k}": Threshold(v, "", "стенд") for k, v in thresholds.items()}
    return CheckContext(
        Camera(slot=1, address="10.0.0.1", username="a"), "", Place.COLOR_BOX, limits
    )


def q(vmaf: float | None, sd: float = 0.5, frames: int = 60) -> Quality:
    values = (vmaf or 0.0,) * frames
    stat = None if vmaf is None else Stat(vmaf, sd, vmaf - sd, values)
    return Quality(
        stat,
        Stat(0.95, 0.01, 0.9, values),
        Stat(40, 1, 38, values),
        frames,
        (1920, 1080),
        "vmaf_v0.6.1",
    )


def curve(vmafs: dict[int, float | None], measured: dict[int, float] | None = None, **fields):
    measured = measured or {}
    points = [Point(k, measured.get(k, k), q(v)) for k, v in vmafs.items()]
    return Curve("H264", 1920, 1080, points=points, **fields)


NORMAL = {512: 71.3, 1024: 80.2, 2048: 88.0, 4096: 93.4, 8192: 96.8}


def test_normal_curve_passes_with_range_and_threshold_source():
    found = verdict(ctx(), curve(NORMAL))
    assert found.status is Status.PASSED
    assert found.reason.startswith("VMAF от 71,3 на 0,5 Мбит/с до 96,8 на 8 Мбит/с")
    assert "порог 1920×1080 — 70,0 (стартовое значение" in found.reason
    assert found.threshold.value == 70.0


def test_below_threshold_on_step_above_floor_fails():
    found = verdict(ctx(), curve(NORMAL | {2048: 58.2, 1024: 55.0, 512: 50.0}))
    assert found.status is Status.FAILED and found.severity is Severity.MAJOR
    assert "качество ниже порога: на 2 Мбит/с VMAF 58,2 при пороге 70,0 для 1920×1080" in (
        found.reason
    )
    # ниже kbps_floor (H.264 1080p — 2048) порог не действует: физика, а не дефект
    assert "на 1 Мбит/с VMAF" not in found.reason


def test_changed_threshold_changes_verdict_without_code():
    assert verdict(ctx(**{"h264.fhd.vmaf_min": 95.0}), curve(NORMAL)).status is Status.FAILED
    weak = NORMAL | {512: 55.0, 1024: 60.0, 2048: 65.0}
    assert verdict(ctx(), curve(weak)).status is Status.FAILED
    assert verdict(ctx(**{"h264.fhd.kbps_floor": 4096.0}), curve(weak)).status is Status.PASSED


def test_quality_not_growing_beyond_spread_fails():
    found = verdict(ctx(), curve(NORMAL | {2048: 84.5, 4096: 81.0}))
    assert found.status is Status.FAILED
    assert (
        "качество не растёт с битрейтом: на 4 Мбит/с VMAF 81,0, на 2 Мбит/с — 84,5; "
        "разница 3,5 больше разброса 2,0" in found.reason
    )


def test_small_dip_within_spread_passes():
    assert verdict(ctx(), curve(NORMAL | {4096: 87.0})).status is Status.PASSED


def test_noisy_frames_widen_spread():
    noisy = curve(NORMAL | {2048: 84.5, 4096: 81.0})
    for point in noisy.points:
        assert point.quality is not None and point.quality.vmaf is not None
        mean = point.quality.vmaf.mean
        shaky = Stat(mean, 6.0, mean - 12, (mean,) * 4)
        point.quality = dataclasses.replace(point.quality, vmaf=shaky, frames=4)
    assert verdict(ctx(), noisy).status is Status.PASSED


def test_bitrate_off_fails_with_deviation_and_tolerance():
    found = verdict(ctx(), curve(NORMAL, measured={2048: 3174.4}))
    assert (
        "битрейт не соответствует заданному: задано 2 Мбит/с, измерено 3,1 Мбит/с" in found.reason
    )
    assert "(+55 % при допуске ±20 %)" in found.reason


def test_variable_bitrate_checks_only_ceiling():
    low = curve(NORMAL, measured={2048: 900.0}, constant=False)
    assert verdict(ctx(), low).status is Status.PASSED
    high = curve(NORMAL, measured={2048: 3000.0}, constant=False)
    assert "при допуске +20 %" in verdict(ctx(), high).reason


def test_refused_and_lost_stream_steps_fail_and_others_continue():
    c = curve(NORMAL)
    c.points[0] = Point(512, refused="камера не приняла значение: записано H.264, 512 кбит/с")
    c.points[2] = Point(2048, no_stream="поток не вернулся после смены настройки")
    found = verdict(ctx(), c)
    assert found.status is Status.FAILED and len(found.problems) == 2
    assert "не приняла" in found.problems[0] and "не вернулся" in found.problems[1]


def test_switch_refused_fails_whole_codec():
    found = verdict(ctx(), Curve("H265", 1920, 1080, switch_refused="камера отказала: Fault"))
    assert found.status is Status.FAILED and found.reason == "камера отказала: Fault"


def test_scene_changed_is_stand_error_even_with_bad_values():
    reason = scene_problem(ctx(), q(88.1))
    assert reason.startswith("сцена менялась: эталоны в начале и в конце профиля совпадают")
    assert "VMAF 88,1 при пороге 90,0" in reason and "повторите прогон" in reason
    found = verdict(ctx(), curve(NORMAL | {2048: 10.0}, scene=reason))
    assert found.status is Status.STAND_ERROR
    assert scene_problem(ctx(), q(97.0)) == ""
    assert scene_problem(ctx(), q(97.0), "средняя яркость сдвинулась на 12,0").startswith(
        "сцена менялась: средняя яркость"
    )


def test_no_vmaf_in_ffmpeg_is_stand_error_but_bitrate_still_fails():
    c = curve(dict.fromkeys(NORMAL))
    assert verdict(ctx(), c).reason == NO_VMAF
    c = curve(dict.fromkeys(NORMAL), measured={1024: 2048.0})
    assert verdict(ctx(), c).status is Status.FAILED


def test_step_without_frames_is_stand_error():
    c = curve(NORMAL)
    c.points[1] = Point(1024, measured_kbps=1024.0)
    assert verdict(ctx(), c).status is Status.STAND_ERROR


@pytest.mark.parametrize(
    "bad", [{"vmaf_spread": 0.0}, {"h265.sd.kbps_floor": -1.0}, {"frames": 10.0}]
)
def test_broken_thresholds_are_stand_error(bad):
    found = verdict(ctx(**bad), curve(NORMAL))
    assert found.status is Status.STAND_ERROR and "проверьте thresholds.yaml" in found.reason


def test_reference_is_the_stream_the_other_loses_more_to():
    assert choose_reference(q(80.0), q(95.0), "H264", "H265", "H264", 2.0)[0] == "H264"
    assert choose_reference(q(95.0), q(80.0), "H264", "H265", "H264", 2.0)[0] == "H265"
    chosen, why = choose_reference(q(94.0), q(95.0), "H264", "H265", "H265", 2.0)
    assert chosen == "H265" and "в пределах разброса" in why
    assert choose_reference(None, q(95.0), "H264", "H265", "H264", 2.0)[0] == "H264"


@pytest.mark.parametrize(
    ("height", "name"), [(240, "sd"), (720, "hd"), (1080, "fhd"), (1088, "fhd"), (1440, "uhd")]
)
def test_vmaf_class_by_height(height, name):
    assert vmaf_class(height) == name


@pytest.mark.parametrize(
    ("kbps", "text"),
    [(256, "256 кбит/с"), (512, "0,5 Мбит/с"), (768, "0,75 Мбит/с"), (8192, "8 Мбит/с")],
)
def test_rate_words(kbps, text):
    assert rate(kbps) == text
