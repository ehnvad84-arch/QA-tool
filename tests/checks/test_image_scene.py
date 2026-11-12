"""Сцена проверки картинки (этап 7, T8): неподвижность и условия для WDR, BLC и шумоподавления."""

import numpy as np

from qa_tool.checks.image.frames import Capture
from qa_tool.checks.image.scene import SCENE_NEEDED, OrdinaryScene, moved
from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Camera, Place, Threshold

RNG = np.random.default_rng(3)


def ctx(**overrides) -> CheckContext:
    thresholds = {f"image.{k}": Threshold(v, "", "тест") for k, v in overrides.items()}
    return CheckContext(
        Camera(slot=1, address="10.0.0.1", username="admin"), "", Place.COLOR_BOX, thresholds
    )


def capture(level=120, noise=0.0, white_share=0.0) -> Capture:
    frame = np.full((60, 80, 3), level, dtype=np.float32)
    rows = int(60 * white_share)
    frame[:rows] = 255
    recent = [
        np.clip(frame + RNG.normal(0, noise, frame.shape), 0, 255).astype(np.uint8)
        for _ in range(8)
    ]
    work = frame.astype(np.uint8)
    return Capture(work, work, recent, 80, 60, "h264", 0.0, True)


def test_before_frames_that_differ_are_stand_error():
    reason = moved(capture(100), capture(110), ctx())
    assert "сцена менялась" in reason and "допуск 3" in reason
    assert moved(capture(100), capture(101), ctx()) == ""
    assert moved(capture(100), capture(110), ctx(scene_diff=20.0)) == ""


def test_wdr_and_blc_need_overexposure_in_ordinary_scene():
    scene, flat = OrdinaryScene(), capture()
    for key in ("wdr", "blc"):
        assert scene.unmet(key, flat, ctx()).startswith(SCENE_NEEDED)
        assert scene.unmet(key, capture(white_share=0.2), ctx()) == ""


def test_noise_reduction_needs_noise():
    scene = OrdinaryScene()
    assert scene.unmet("nr", capture(noise=0.0), ctx()).startswith(SCENE_NEEDED)
    assert scene.unmet("nr", capture(noise=5.0), ctx()) == ""


def test_other_settings_need_nothing_and_scene_is_ready():
    scene = OrdinaryScene()
    assert all(scene.unmet(key, capture(), ctx()) == "" for key in ("brightness", "wb_red"))
    assert scene.prepare(ctx()) is None
    assert scene.regions((60, 80, 3))["кадр"].all()
