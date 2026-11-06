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
        "RUV-S-STREAM-1": ("прошла", ""),
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


def test_replay_is_proved_only_by_real_playback():
    replay = next(f for f in profiles.PROFILES["G"] if f.spec == "Replay Service")
    rows = {
        "DEVICE-1-1-20": ("прошла", ""),
        "RUV-G-REPLAY-1": ("пропущена", "на карте нет записей"),
    }
    assert profiles.judge(replay, rows)[0] == profiles.UNCHECKED


def test_broken_search_is_seen_even_if_other_search_tests_passed():
    search = next(f for f in profiles.PROFILES["G"] if f.spec == "Recording Search Service")
    rows = {"SEARCH-1-1-1": ("прошла", ""), "RUV-G-SEARCH-1": ("не прошла", "нет токена")}
    assert profiles.judge(search, rows)[0] == profiles.BROKEN
    assert profiles.judge(search, {"SEARCH-1-1-1": ("прошла", "")})[0] == profiles.UNCHECKED


def test_profile_with_unproved_archive_is_not_passed(camera_sg):
    previous = {
        f"{mask.replace('*', '1')}": ("прошла", "")
        for feature in profiles.PROFILES["G"]
        for mask in feature.tests
    }
    previous["RUV-G-REPLAY-1"] = ("пропущена", "на карте нет записей")
    camera = Camera(slot=1, address="127.0.0.1", port=camera_sg.port, username="admin")
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX, extra={PREVIOUS_RESULTS: previous})
    row = next(c for c in run_check(SPEC, ctx).children if c.check_id == "RUV-PROFILE-G")
    assert row.result.status is Status.SKIPPED
    assert "Воспроизведение архива" in row.result.reason


def test_profile_is_not_passed_when_onvif_tt_gave_no_rows(camera_sg):
    camera = Camera(slot=1, address="127.0.0.1", port=camera_sg.port, username="admin")
    previous = {"RUV-G-SEARCH-1": ("прошла", ""), "RUV-G-REPLAY-1": ("прошла", "")}  # onvif-tt упал
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX, extra={PREVIOUS_RESULTS: previous})
    row = next(c for c in run_check(SPEC, ctx).children if c.check_id == "RUV-PROFILE-G")
    assert row.result.status is Status.STAND_ERROR
    assert "onvif-tt" in row.result.reason


def test_rows_of_excluded_tests_and_stand_errors_are_not_a_run_of_onvif_tt():
    assert not profiles.ran_onvif_tt({"EVENT-2-1-9": ("пропущена", "исключён")})
    assert not profiles.ran_onvif_tt({"DEVICE-1-1-2": ("ошибка стенда", "нет связи")})
    assert not profiles.ran_onvif_tt({"RUV-G-REPLAY-1": ("прошла", "")})
    assert profiles.ran_onvif_tt({"DEVICE-1-1-2": ("не прошла", "")})


def test_stand_error_of_another_row_does_not_cancel_real_playback():
    replay = next(f for f in profiles.PROFILES["G"] if f.spec == "Replay Service")
    rows = {"RUV-G-REPLAY-1": ("прошла", ""), "DEVICE-1-1-20": ("ошибка стенда", "")}
    assert profiles.judge(replay, rows)[0] == profiles.WORKS


def test_stand_error_in_proof_stays_a_stand_error(camera_sg):
    camera = Camera(slot=1, address="127.0.0.1", port=camera_sg.port, username="admin")
    previous = {
        "DEVICE-1-1-13": ("прошла", ""),
        "RUV-G-SEARCH-1": ("прошла", ""),
        "RUV-G-REPLAY-1": ("ошибка стенда", "таймаут"),
    }
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX, extra={PREVIOUS_RESULTS: previous})
    row = next(c for c in run_check(SPEC, ctx).children if c.check_id == "RUV-PROFILE-G")
    assert row.result.status is Status.STAND_ERROR


def test_declared_profile_without_any_checked_feature_is_not_passed(camera_sg):
    camera = Camera(slot=1, address="127.0.0.1", port=camera_sg.port, username="admin")
    ctx = CheckContext(camera, "admin", Place.COLOR_BOX, extra={PREVIOUS_RESULTS: {}})
    rows = {c.check_id: c.result for c in run_check(SPEC, ctx).children}
    assert rows["RUV-PROFILE-S"].status is Status.STAND_ERROR  # onvif-tt упал — нет доказательства
    assert "ни одна обязательная функция не проверена" in rows["RUV-PROFILE-S"].reason


def test_scopes_matched_regardless_of_case_and_trailing_slash(monkeypatch):
    class Answer:
        def iter(self):
            for text in (
                "ONVIF://www.onvif.org/Profile/streaming/",
                "onvif://www.onvif.org/Profile/G",
            ):
                yield type("E", (), {"tag": "{x}ScopeItem", "text": text})()

    monkeypatch.setattr(profiles.SoapClient, "call", lambda self, url, body: Answer())
    camera = Camera(slot=1, address="127.0.0.1", port=1, username="admin")
    assert profiles.declared_profiles(CheckContext(camera, "x", Place.COLOR_BOX)) == {"S", "G"}
