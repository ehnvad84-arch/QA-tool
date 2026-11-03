import re
from pathlib import Path

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import profiles
from qa_tool.core.checks import PREVIOUS_RESULTS, REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from tests.mock_camera import MockCamera, Passport
from tests.mock_onvif import OnvifState

load_all()
SPEC = REGISTRY.get(profiles.CHECK_ID)
SPEC_FILE = Path(__file__).resolve().parents[1] / "data" / "onvif_profiles_conformance.xml"


def support_check_text(profile: str) -> str:
    s = SPEC_FILE.read_text(encoding="utf-8-sig")
    start = s.find(f"<title>Profile {profile} Support Check</title>")
    end = s.find("<title>Profile", start + 50)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s[start:end]))


@pytest.mark.parametrize("profile", list(profiles.PROFILES))
def test_curated_features_match_official_spec(profile):
    text = support_check_text(profile)
    missing = [f.spec for f in profiles.PROFILES[profile] if f.spec not in text]
    assert not missing, f"Profile {profile}: в Support Check нет фраз {missing}"


def test_judge_verdicts():
    replay = next(f for f in profiles.PROFILES["G"] if f.spec == "Replay Service")
    assert profiles.judge(replay, {"RUV-G-REPLAY-1": ("прошла", "")})[0] == profiles.WORKS
    assert (
        profiles.judge(
            replay, {"RUV-G-REPLAY-1": ("не прошла", "нет кадров"), "DEVICE-1-1-20": ("прошла", "")}
        )[0]
        == profiles.BROKEN
    )
    assert (
        profiles.judge(
            replay,
            {
                "DEVICE-1-1-20": (
                    "пропущена",
                    "onvif-tt пропустил: DUT does not advertise services: ['replay']",
                )
            },
        )[0]
        == profiles.UNSUPPORTED
    )
    assert profiles.judge(replay, {})[0] == profiles.UNCHECKED


@pytest.fixture
def camera_sg():
    cam = MockCamera(Passport("X", "M", "1", "S1"), state=OnvifState(profiles=("S", "G"))).start()
    yield cam
    cam.stop()


def test_declared_profile_with_broken_mandatory_feature_is_task(camera_sg):
    camera = Camera(slot=1, address="127.0.0.1", port=camera_sg.port, username="admin")
    previous = {
        "DEVICE-1-1-2": ("прошла", ""),
        "LOCAL-MEDIA-S-RTSP-LIVE": ("прошла", ""),
        "RUV-G-REPLAY-1": ("не прошла", "архив не отдан"),
        "SEARCH-1-1-1": ("прошла", ""),
    }
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX, extra={PREVIOUS_RESULTS: previous})
    result = run_check(SPEC, ctx)
    rows = {c.check_id: c.result for c in result.children}
    assert rows["RUV-PROFILE-S"].status is Status.PASSED
    assert rows["RUV-PROFILE-G"].status is Status.FAILED
    assert "Воспроизведение архива — не работает" in rows["RUV-PROFILE-G"].reason
    assert (
        rows["RUV-PROFILE-M"].status is Status.SKIPPED
        and "не объявлен" in rows["RUV-PROFILE-M"].reason
    )
    assert rows["RUV-PROFILE-G"].evidence[0].name == "matrix.json"


def test_profiles_section_in_protocol(tmp_path, camera_sg):
    from openpyxl import load_workbook

    from qa_tool.core.models import Scenario, Stand
    from qa_tool.core.store import RunStore
    from qa_tool.report.html import render_protocol
    from qa_tool.report.model import build_protocol
    from qa_tool.report.xlsx import write_protocol_xlsx

    camera = Camera(slot=1, address="127.0.0.1", port=camera_sg.port, username="admin")
    stand = Stand(place=Place.COLOR_BOX, runs_dir=tmp_path / "runs", cameras=[camera])
    store = RunStore.create(stand, Scenario())
    ctx = CheckContext(
        camera,
        "admin",
        Place.COLOR_BOX,
        extra={PREVIOUS_RESULTS: {"RUV-G-REPLAY-1": ("прошла", "")}},
    )
    store.add_result(1, SPEC, run_check(SPEC, ctx), "t")
    protocol = build_protocol(store, 1)
    assert protocol.profiles["declared"] == ["G", "S"]
    html = render_protocol(protocol)
    assert "Профили ONVIF" in html and "Воспроизведение архива" in html
    path = write_protocol_xlsx(tmp_path / "p.xlsx", protocol)
    assert "Профили ONVIF" in load_workbook(path).sheetnames
