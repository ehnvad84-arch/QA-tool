"""Правило итогов картинки (этап 7, T7): случаи из спеки и пороги из конфигурации."""

import pytest

from qa_tool.checks.image.rules import DEFAULTS, rule_for, verdict
from qa_tool.checks.image.settings import SETTINGS
from qa_tool.core.checks import CheckContext, Severity, Status
from qa_tool.core.models import Camera, Place, Threshold


def ctx(**overrides) -> CheckContext:
    thresholds = {k.replace("__", "."): Threshold(v, "", "тест") for k, v in overrides.items()}
    return CheckContext(
        Camera(slot=1, address="10.0.0.1", username="admin"), "", Place.COLOR_BOX, thresholds
    )


NO_CLIP = [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    ("key", "values", "mode", "code", "status"),
    [
        ("contrast", [20, 30, 40], "MANUAL", "effect", Status.PASSED),
        ("contrast", [30, 30.5, 31], "MANUAL", "none", Status.FAILED),
        ("contrast", [30, 30.5, 31], "AUTO", "skipped_ae", Status.SKIPPED),
        ("contrast", [30, 30.5, 31], None, "skipped_ae", Status.SKIPPED),
        ("saturation", [30, 30.5, 31], "AUTO", "none", Status.FAILED),  # АЭ её не гасит
        ("contrast", [40, 30, 20], "MANUAL", "reverse", Status.FAILED),
        ("contrast", [20, 40, 30], "MANUAL", "uneven", Status.FAILED),
        ("contrast", [20, 21, 40], "MANUAL", "partial", Status.FAILED),
        ("wdr", [80, 70, 60], "MANUAL", "effect", Status.PASSED),  # WDR сжимает размах
        ("nr", [4.0, 3.0, 2.0], "AUTO", "effect", Status.PASSED),  # шум в % от исходного
        (
            "sharpness",
            [100, 105, 115],
            "AUTO",
            "partial",
            Status.FAILED,
        ),  # 5 % < 10 % на первой половине
    ],
)
def test_cases_of_the_verdict_rule(key, values, mode, code, status):
    found = verdict(rule_for(ctx(), key), values, NO_CLIP, mode)
    assert (found.code, found.status) == (code, status), found.reason


def test_partial_effect_is_minor_and_clipping_excuses_it():
    rule = rule_for(ctx(), "brightness")
    partial = verdict(rule, [100, 140, 142], NO_CLIP, "MANUAL")
    assert partial.severity is Severity.MINOR
    clipped = verdict(rule, [100, 140, 142], [0, 0, 40], "MANUAL")
    assert clipped.status is Status.PASSED and "упор в пределы сцены" in clipped.reason


def test_two_positions_off_on():
    rule = rule_for(ctx(), "wdr")
    assert verdict(rule, [80, 60], NO_CLIP, "MANUAL").status is Status.PASSED
    assert verdict(rule, [80, 79.5], NO_CLIP, "MANUAL").code == "none"
    assert verdict(rule, [60, 80], NO_CLIP, "MANUAL").code == "reverse"


def test_scene_and_backlight_manual_exposure_are_skipped():
    blc = rule_for(ctx(), "blc")
    assert verdict(blc, [50, 60, 70], NO_CLIP, "MANUAL").code == "skipped_ae"
    assert verdict(blc, [50, 60, 70], NO_CLIP, "AUTO").code == "effect"
    assert verdict(blc, [50, 60, 70], NO_CLIP, "AUTO", scene_ok=False).code == "skipped_scene"


def test_step_not_above_stability_is_stand_error():
    bad = rule_for(ctx(image__contrast__step=0.5), "contrast")
    found = verdict(bad, [20, 30, 40], NO_CLIP, "MANUAL")
    assert found.status is Status.STAND_ERROR and "thresholds.yaml" in found.reason
    wrong_side = rule_for(ctx(image__contrast__direction=0), "contrast")
    assert verdict(wrong_side, [20, 30, 40], NO_CLIP, "MANUAL").code == "bad_rule"


def test_threshold_in_configuration_changes_the_verdict():
    values = [20, 25, 30]
    assert verdict(rule_for(ctx(), "contrast"), values, NO_CLIP, "MANUAL").code == "effect"
    strict = rule_for(ctx(image__contrast__step=6), "contrast")
    assert verdict(strict, values, NO_CLIP, "MANUAL").code == "partial"
    assert strict.step.source == "тест"


def test_nan_or_zero_reference_is_stand_error():
    assert verdict(rule_for(ctx(), "contrast"), [20, float("nan"), 30], NO_CLIP, "MANUAL").code == (
        "no_value"
    )
    assert verdict(rule_for(ctx(), "sharpness"), [0, 10, 20], NO_CLIP, "MANUAL").code == "no_value"


def test_every_setting_has_start_rules_with_source():
    for setting in SETTINGS:
        rule = rule_for(ctx(), setting.key)
        assert not rule.problem, setting.key
        assert "уточнить" in rule.step.source
        assert rule.expected
    assert DEFAULTS["image.clip_pct"].value == 5.0
