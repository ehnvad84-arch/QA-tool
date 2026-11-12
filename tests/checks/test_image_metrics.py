"""Величины картинки на синтетических кадрах (этап 7, T6): каждая растёт в свою сторону и не
замечает чужих изменений."""

import numpy as np
import pytest

from qa_tool.checks.image import metrics
from qa_tool.checks.image.frames import Capture
from qa_tool.checks.image.settings import SETTINGS

RNG = np.random.default_rng(7)


def scene(height=120, width=160) -> np.ndarray:
    """Цветная сцена с краями и плавными переходами, без упора в пересвет и черноту."""
    y, x = np.mgrid[0:height, 0:width]
    r = 60 + 100 * (x / width)
    g = 60 + 100 * (y / height)
    b = 140 - 60 * (x / width)
    frame = np.stack([r, g, b], axis=-1)
    frame[40:80, 50:110] += 30  # объект с резкими краями
    return np.clip(frame, 20, 230).astype(np.uint8)


def shifted(frame, delta):
    return np.clip(frame.astype(np.int16) + delta, 0, 255).astype(np.uint8)


def contrasted(frame, k):
    mid = frame.astype(np.float32).mean()
    return np.clip((frame - mid) * k + mid, 0, 255).astype(np.uint8)


def saturated(frame, k):
    gray = frame.astype(np.float32).mean(axis=-1, keepdims=True)
    return np.clip(gray + (frame - gray) * k, 0, 255).astype(np.uint8)


def blurred(frame):
    f = frame.astype(np.float32)
    out = f.copy()
    out[1:-1, 1:-1] = (f[:-2, 1:-1] + f[2:, 1:-1] + f[1:-1, :-2] + f[1:-1, 2:] + f[1:-1, 1:-1]) / 5
    return out.astype(np.uint8)


def noisy(frame, sigma, count=8):
    return [
        np.clip(frame + RNG.normal(0, sigma, frame.shape), 0, 255).astype(np.uint8)
        for _ in range(count)
    ]


def test_brightness_grows_and_contrast_ignores_a_shift():
    base = scene()
    assert metrics.mean_luma(shifted(base, 20)) > metrics.mean_luma(base) + 15
    assert metrics.luma_std(shifted(base, 20)) == pytest.approx(metrics.luma_std(base), abs=0.5)


def test_contrast_grows_and_brightness_barely_moves():
    base = scene()
    stronger = contrasted(base, 1.5)
    assert metrics.luma_std(stronger) > metrics.luma_std(base) * 1.3
    assert metrics.mean_luma(stronger) == pytest.approx(metrics.mean_luma(base), abs=2)


def test_saturation_grows_and_ignores_brightness_scale():
    base = scene()
    assert metrics.saturation(saturated(base, 1.6)) > metrics.saturation(base) + 5
    darker = (base.astype(np.float32) * 0.8).astype(np.uint8)
    assert metrics.saturation(darker) == pytest.approx(metrics.saturation(base), abs=1.0)


def test_sharpness_drops_on_blur_and_ignores_a_shift():
    base = scene()
    assert metrics.laplacian_var(blurred(base)) < metrics.laplacian_var(base) * 0.7
    assert metrics.laplacian_var(shifted(base, 15)) == pytest.approx(
        metrics.laplacian_var(base), rel=0.05
    )


def test_red_blue_follows_gains_and_ignores_brightness():
    base = scene()
    redder = base.copy()
    redder[..., 0] = np.clip(base[..., 0] * 1.2, 0, 255).astype(np.uint8)
    bluer = base.copy()
    bluer[..., 2] = np.clip(base[..., 2] * 1.2, 0, 255).astype(np.uint8)
    assert metrics.red_blue(redder) > metrics.red_blue(base) * 1.1
    assert metrics.red_blue(bluer) < metrics.red_blue(base) * 0.9
    darker = (base.astype(np.float32) * 0.8).astype(np.uint8)
    assert metrics.red_blue(darker) == pytest.approx(metrics.red_blue(base), rel=0.03)


def test_wdr_spread_shrinks_with_compression_on_fixed_masks():
    base = scene()
    masks = metrics.masks_of(base)
    compressed = contrasted(base, 0.6)
    assert metrics.luma_spread(compressed, masks) < metrics.luma_spread(base, masks) - 10
    assert metrics.luma_spread(shifted(base, 10), masks) == pytest.approx(
        metrics.luma_spread(base, masks), abs=1.0
    )


def test_center_luma_reacts_to_center_only():
    base = scene()
    lit = base.copy()
    lit[40:80, 53:107] = shifted(base[40:80, 53:107], 30)
    edges = base.copy()
    edges[:20] = shifted(base[:20], 30)
    assert metrics.center_luma(lit) > metrics.center_luma(base) + 10
    assert metrics.center_luma(edges) == pytest.approx(metrics.center_luma(base), abs=0.5)


def test_noise_drops_with_denoise_and_ignores_static_shift():
    base = scene()
    loud, quiet = noisy(base, 8), noisy(base, 2)
    assert metrics.temporal_noise(quiet) < metrics.temporal_noise(loud) * 0.5
    brighter = noisy(shifted(base, 15), 8)
    assert metrics.temporal_noise(brighter) == pytest.approx(metrics.temporal_noise(loud), rel=0.15)


def test_clipping_is_counted():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    frame[:5] = 255
    clip, crush = metrics.clipped(frame)
    assert (clip, crush) == (50.0, 50.0)


def test_fully_clipped_frame_gives_nan_not_a_crash():
    white = np.full((10, 10, 3), 255, dtype=np.uint8)
    assert np.isnan(metrics.saturation(white)) and np.isnan(metrics.red_blue(white))


def test_every_catalog_metric_is_measurable():
    base = scene()
    capture = Capture(base, base, noisy(base, 3), 160, 120, "h264", 0.0, True)
    for setting in SETTINGS:
        result = metrics.measure(setting.metric, capture)
        assert np.isfinite(result.value), setting.key
    with pytest.raises(KeyError):
        metrics.measure("нет такой", capture)
