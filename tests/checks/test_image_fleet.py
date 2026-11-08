"""IMAGE-EFFECT-1 в синхронной фазе на 8 виртуальных камерах (этап 7, T12)."""

import time

import pytest
from openpyxl import load_workbook

from qa_tool.checks.image.engine import CHECK_ID, RESTORE_ID, STATE_ID
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.scheduler import Runner
from qa_tool.report.publish import publish
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.checks.test_image_effect import FAST, MANUAL
from tests.mock_camera import VENDORS, MockCamera, Passport
from tests.mock_onvif import IMAGING_OPTIONS, OnvifState

pytestmark = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None or tool_path("ffprobe") is None, reason="нет ffmpeg"
)


@pytest.fixture
def fleet(monkeypatch):
    cameras = [
        MockCamera(
            Passport(VENDORS[i % len(VENDORS)], f"MOCK-{i + 1}", "1.0", f"SN{i + 1:04d}"),
            state=OnvifState(
                imaging_xml=MANUAL,
                imaging_video=True,
                # у второй камеры другой диапазон: сравнение идёт по процентам диапазона
                imaging_options_xml=IMAGING_OPTIONS.replace(
                    "<tt:Brightness><tt:Min>0</tt:Min><tt:Max>100</tt:Max>",
                    "<tt:Brightness><tt:Min>-50</tt:Min><tt:Max>200</tt:Max>",
                )
                if i == 1
                else IMAGING_OPTIONS,
            ),
        ).start()
        for i in range(8)
    ]
    for slot in range(1, 9):
        monkeypatch.setenv(f"QA_TOOL_PASSWORD_{slot}", "admin")
    yield cameras
    for cam in cameras:
        cam.stop()


def test_eight_cameras_at_once_give_rows_for_nine_settings_and_progress(tmp_path, fleet):
    cameras = [
        Camera(
            slot=i,
            address="127.0.0.1",
            port=m.port,
            rtsp_port=m.rtsp_port,
            username="admin",
            vendor=m.passport.manufacturer.lower(),
        )
        for i, m in enumerate(fleet, start=1)
    ]
    stand = Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=cameras)
    events = []
    thresholds = {k: Threshold(v, "", "тест") for k, v in FAST.items()}
    runner = Runner(
        stand,
        Scenario(checks=[CHECK_ID], allow_writes=True),
        thresholds=thresholds,
        on_event=events.append,
    )
    start = time.monotonic()
    store = runner.run()
    elapsed = time.monotonic() - start
    for slot in range(1, 9):
        found = {r["check_id"]: r["status"] for r in store.results(slot=slot)}
        image = {k: v for k, v in found.items() if k.startswith("IMAGE-")}
        assert len(image) == 11, (slot, found)
        assert image[STATE_ID] == "прошла" and image[RESTORE_ID] == "прошла", (slot, image)
        assert image["IMAGE-BRIGHT-1"] == "прошла", (slot, image)
    progress = [e for e in events if e.kind == "progress"]
    assert {e.slot for e in progress} == set(range(1, 9))
    assert all(e.check_id == CHECK_ID for e in progress)
    assert elapsed < 1700, elapsed
    for cam in fleet:
        assert cam.state.imaging_xml == MANUAL  # каждая камера ушла такой, какой пришла
    publish(store)  # T17: сравнение картинки выпускается вместе с протоколами
    run_dir = store.run_dir
    for suffix in ("html", "pdf", "xlsx"):
        assert (run_dir / f"image_compare.{suffix}").exists()
    html = (run_dir / "image_compare.html").read_text(encoding="utf-8")
    assert html.count('class="page"') == 9  # матрица и лист на каждую из 9 настроек
    assert 'src="compare/IMAGE-BRIGHT-1.png"' in html
    assert "image_compare.html" in (run_dir / "summary.html").read_text(encoding="utf-8")
    bright = load_workbook(run_dir / "image_compare.xlsx")["IMAGE-BRIGHT-1"]
    effects = [row[2] for row in bright.iter_rows(min_row=2, values_only=True)]
    assert effects == ["прошла"] * 8  # М2 с диапазоном −50…200 — тоже по процентам
