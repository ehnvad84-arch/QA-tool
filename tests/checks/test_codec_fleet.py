"""CODEC-QUALITY-1 в синхронной фазе на 8 виртуальных камерах вместе с «Картинкой» (этап 8, T12).

Тест держит нагрузку фазы: 8 рабочих процессов камер, у каждой — выборки RTSP и ffmpeg расчёта
качества. Время фазы и пик памяти печатаются: их записывают в progress.md фичи.
"""

import time

import pytest
from openpyxl import load_workbook

from qa_tool.checks.codec.engine import CHECK_ID, RESTORE_ID, STATE_ID
from qa_tool.checks.codec.quality import has_libvmaf
from qa_tool.checks.image.engine import CHECK_ID as IMAGE_ID
from qa_tool.core.models import Camera, Place, Scenario, Stand, Threshold
from qa_tool.core.scheduler import Runner
from qa_tool.report.publish import publish
from qa_tool.tools import tool_path
from tests import mock_rtsp
from tests.checks.test_codec_engine import FAST
from tests.checks.test_image_effect import FAST as IMAGE_FAST
from tests.checks.test_image_effect import MANUAL
from tests.mock_camera import VENDORS, MockCamera, Passport
from tests.mock_onvif import MockStream, OnvifState, encoder_now

pytestmark = pytest.mark.skipif(
    mock_rtsp.find_ffmpeg() is None
    or tool_path("ffprobe") is None
    or not has_libvmaf(tool_path("ffmpeg") or ""),
    reason="нет ffmpeg с libvmaf",
)
PHASE_LIMIT_S = 20 * 60  # спека, «Время и ресурсы»: фаза кодека не дольше 20 мин


@pytest.fixture
def fleet(monkeypatch):
    codecs = ("H264", "H265") if mock_rtsp.hevc_available() else ("H264",)
    cameras = [
        MockCamera(
            Passport(VENDORS[i % len(VENDORS)], f"MOCK-{i + 1}", "1.0", f"SN{i + 1:04d}"),
            # у двух камер оба кодека: смена кодека под нагрузкой соседей
            state=OnvifState(
                imaging_xml=MANUAL,
                encoder_video=True,
                encoder_codecs=codecs if i < 2 else ("H264",),
            ),
        ).start()
        for i in range(8)
    ]
    for slot in range(1, 9):
        monkeypatch.setenv(f"QA_TOOL_PASSWORD_{slot}", "admin")
    yield cameras
    for cam in cameras:
        cam.stop()


def test_eight_cameras_measure_codecs_with_image_in_sync_phase(tmp_path, fleet):
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
    limits = {**IMAGE_FAST, **FAST}
    thresholds = {k: Threshold(v, "", "тест") for k, v in limits.items()}
    events = []
    runner = Runner(
        stand,
        Scenario(checks=[IMAGE_ID, CHECK_ID], allow_writes=True),
        thresholds=thresholds,
        on_event=events.append,
    )
    start = time.monotonic()
    store = runner.run()
    elapsed = time.monotonic() - start
    for slot in range(1, 9):
        found = {r["check_id"]: r for r in store.results(slot=slot)}
        assert found[STATE_ID]["status"] == "прошла", (slot, found[STATE_ID]["reason"])
        assert found[RESTORE_ID]["status"] == "прошла", (slot, found[RESTORE_ID]["reason"])
        row = found["CODEC-H264-1"]
        assert row["status"] == "прошла", (slot, row["reason"])
        assert "VMAF" in row["actual"]  # числа есть, а не «ошибка стенда»
        assert found["IMAGE-RESTORE-1"]["status"] == "прошла", slot
    for cam in fleet:  # каждая камера ушла такой, какой пришла: H.264, 4000 кбит/с, переменный
        now = encoder_now(cam.state, MockStream(cam.state.profile_token))
        assert (now["encoding"], now["kbps"], now["constant"]) == ("H264", 4000, False)
    codec_progress = [e for e in events if e.kind == "progress" and e.check_id == CHECK_ID]
    assert {e.slot for e in codec_progress} == set(range(1, 9))
    resource = pytest.importorskip("resource")  # замер памяти — только на POSIX
    peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    print(f"\nфаза «Картинка» + «Кодеки», 8 камер: {elapsed:.0f} с, пик процесса {peak_mb:.0f} МБ")
    assert elapsed < PHASE_LIMIT_S, elapsed
    publish(store)  # T16: сравнение кодеков выпускается вместе с протоколами
    run_dir = store.run_dir
    for suffix in ("html", "pdf", "xlsx"):
        assert (run_dir / f"codec_compare.{suffix}").exists()
    html = (run_dir / "codec_compare.html").read_text(encoding="utf-8")
    assert "320×240 · H.264" in html and html.count('class="page"') >= 1
    assert (run_dir / "compare" / "codec_1.png").exists()
    assert 'href="codec_compare.html"' in (run_dir / "summary.html").read_text(encoding="utf-8")
    sheet = load_workbook(run_dir / "codec_compare.xlsx")["Точки"]
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert {r[0] for r in rows} == {f"М{slot}" for slot in range(1, 9)}
    assert all(isinstance(r[7], int | float) for r in rows)  # VMAF — числом
