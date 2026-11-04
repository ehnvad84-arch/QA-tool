import json

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.api import ruver
from qa_tool.checks.api.ruver import CHECK_ID, LEFT_CHANGED, RESTORE_ID
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import RUVER, Camera, Place, Scenario
from qa_tool.drivers.camera.ruver.client import (
    DESTRUCTIVE,
    NOT_CALLED,
    READ,
    TABOO,
    WRITE,
    load_map,
)
from tests.mock_ruver import MockRuver

load_all()
API = load_map()
SPEC = REGISTRY.get(CHECK_ID)
BRIGHTNESS = "/api/image/brightness"


@pytest.fixture
def camera():
    mock = MockRuver().start()
    yield mock
    mock.stop()


def ctx_for(camera, password="admin", **options):
    place = Camera(slot=1, address="127.0.0.1", port=camera.port, username="admin", vendor=RUVER)
    extra = {"api_timeout_s": 0.3, "api_reboot_poll_s": 0.01, **options.pop("extra", {})}
    return CheckContext(place, password, Place.COLOR_BOX, extra=extra, **options)


def rows_of(result):
    return {child.check_id: child.result for child in result.children}


def row(result, method, path):
    return rows_of(result)[API.get(method, path).id]


def run(camera, **options):
    return run_check(SPEC, ctx_for(camera, **options))


def changing_requests(camera):
    """Всё, что дошло до камеры и меняет её: POST вне входа и чтений."""
    allowed = {"/api/auth/login", "/api/auth/logout"}
    allowed |= {op.path for op in API.operations if op.kind == READ and op.method == "POST"}
    return [r for r in camera.requests if r.method == "POST" and r.path not in allowed]


# --- S1, S2, S3: сеанс -------------------------------------------------------------------------


def test_one_session_and_it_is_closed(camera):
    run(camera)
    assert camera.login_attempts == 1
    assert len(camera.closed_sessions) == 1
    assert camera.sessions == {}


def test_wrong_password_one_attempt_no_tasks(camera):
    result = run(camera, password="wrong")
    assert result.status is Status.SKIPPED
    assert camera.login_attempts == 1
    assert not result.children


def test_no_free_session_is_stand_error(camera):
    camera.busy_sessions = 3
    result = run(camera)
    assert result.status is Status.STAND_ERROR
    assert "нет свободного сеанса" in result.reason


def test_session_closed_by_camera_is_reopened(camera):
    camera.expire_session_after = 10
    result = run(camera)
    assert camera.login_attempts == 2
    assert not [r for r in result.children if r.result.status is Status.STAND_ERROR]


# --- S4: чтение --------------------------------------------------------------------------------


def test_every_read_called_once_without_writes(camera):
    result = run(camera)
    assert changing_requests(camera) == []
    for op in API.operations:
        if op.kind == READ and not op.missing:
            assert len(camera.sent(op.method, op.path)) == 1, op.path
    assert row(result, "GET", "/api/info").status is Status.PASSED


def test_row_for_every_operation(camera):
    result = run(camera)
    assert {op.id for op in API.operations} <= set(rows_of(result))


def test_corrupt_answer_fails_with_evidence(camera):
    camera.corrupt.add(BRIGHTNESS)
    found = row(run(camera), "GET", BRIGHTNESS)
    assert found.status is Status.FAILED
    assert "схем" in found.reason
    assert found.evidence


def test_required_parameter_without_value_is_skipped(camera):
    found = row(run(camera), "GET", "/api/events/get_event")
    assert found.status is Status.SKIPPED
    assert "нужны параметры" in found.reason
    assert not camera.sent("GET", "/api/events/get_event")


def test_forbidden_answer_is_stand_error(camera):
    camera.forbidden.add(BRIGHTNESS)
    assert row(run(camera), "GET", BRIGHTNESS).status is Status.STAND_ERROR


def test_timeout_is_stand_error(camera):
    camera.slow.add(BRIGHTNESS)
    assert row(run(camera), "GET", BRIGHTNESS).status is Status.STAND_ERROR


def test_binary_answer_keeps_only_size_and_hash(camera):
    found = row(run(camera), "GET", "/api/video/snapshot")
    assert found.status is Status.PASSED
    assert "sha256" in found.evidence[0].content
    assert "binary" not in found.evidence[0].content


def test_possible_secrets_stay_out_of_evidence(camera):
    camera.state["/api/configuration"]["configuration"] = "secret-config-text"
    found = row(run(camera), "GET", "/api/configuration")
    assert "secret-config-text" not in found.evidence[0].content


def test_password_is_cut_from_evidence(camera):
    camera.state["/api/info"]["cameraName"] = "admin"
    found = row(run(camera), "GET", "/api/info")
    assert '"cameraName": "admin"' not in found.evidence[0].content


# --- S5 ----------------------------------------------------------------------------------------


def test_address_missing_in_firmware_is_skipped_not_a_task(camera):
    camera.state["/api/info"]["firmwareVersion"] = "0.6.14"
    camera.missing.add("/api/hardware")
    found = row(run(camera), "GET", "/api/hardware")
    assert found.status is Status.SKIPPED
    assert "нет в прошивке 0.6.14" in found.reason


# --- S6: запись --------------------------------------------------------------------------------


def test_write_reads_back_and_returns(camera):
    before = camera.field_value(BRIGHTNESS, "brightness")
    result = run(camera, allow_writes=True)
    assert row(result, "POST", BRIGHTNESS).status is Status.PASSED
    values = [r.body["brightness"] for r in camera.sent("POST", BRIGHTNESS)]
    assert values == [before + 1, before, 101]  # новое, исходное, вне пределов
    assert camera.field_value(BRIGHTNESS, "brightness") == before
    assert rows_of(result)[RESTORE_ID].status is Status.PASSED


def test_camera_ends_as_it_began(camera):
    before = json.dumps(camera.state, sort_keys=True)
    run(camera, allow_writes=True)
    assert json.dumps(camera.state, sort_keys=True) == before


def test_ignored_write_fails(camera):
    camera.ignored_writes.add(BRIGHTNESS)
    found = row(run(camera, allow_writes=True), "POST", BRIGHTNESS)
    assert found.status is Status.FAILED
    assert "прочитано" in found.reason


def test_stuck_value_is_reported_as_left_changed(camera):
    camera.stuck.add(BRIGHTNESS)
    result = run(camera, allow_writes=True)
    found = row(result, "POST", BRIGHTNESS)
    assert found.status is Status.FAILED
    assert LEFT_CHANGED in found.reason
    assert rows_of(result)[RESTORE_ID].status is Status.FAILED
    assert LEFT_CHANGED in rows_of(result)[RESTORE_ID].reason


def test_lost_answer_after_write_still_returns_value(camera):
    seen = []

    def lose_first_answer(mock, logged):
        if logged.method == "POST" and logged.path == BRIGHTNESS:
            seen.append(logged)
            (mock.slow.add if len(seen) == 1 else mock.slow.discard)(BRIGHTNESS)
        elif seen:
            mock.slow.discard(BRIGHTNESS)

    camera.on_request = lose_first_answer
    before = camera.field_value(BRIGHTNESS, "brightness")
    found = row(run(camera, allow_writes=True), "POST", BRIGHTNESS)
    assert found.status is Status.STAND_ERROR
    assert camera.field_value(BRIGHTNESS, "brightness") == before


def test_setting_that_needs_reboot_is_not_written(camera):
    camera.reboot_needed.add(BRIGHTNESS)
    found = row(run(camera, allow_writes=True), "POST", BRIGHTNESS)
    assert found.status is Status.SKIPPED
    assert "применяется после перезагрузки" in found.reason
    assert not camera.sent("POST", BRIGHTNESS)


def test_side_effect_on_a_write_setting_is_returned(camera):
    contrast = "/api/image/contrast"
    before = camera.field_value(contrast, "contrast")
    camera.side_effects[BRIGHTNESS] = (contrast, "contrast", before + 7)
    # контраст не пишется сам: вернуть его может только сверка после записи
    camera.reboot_needed.add(contrast)
    result = run(camera, allow_writes=True)
    assert camera.field_value(contrast, "contrast") == before
    assert rows_of(result)[RESTORE_ID].status is Status.PASSED


def test_side_effect_on_a_read_only_field_left_changed(camera):
    camera.side_effects[BRIGHTNESS] = (
        "/api/hardware",
        next(iter(camera.state["/api/hardware"])),
        "z",
    )
    result = run(camera, allow_writes=True)
    found = rows_of(result)[RESTORE_ID]
    assert found.status is Status.FAILED
    assert "/api/hardware" in found.reason


def test_no_writes_without_permission(camera):
    result = run(camera)
    found = row(result, "POST", BRIGHTNESS)
    assert found.status is Status.SKIPPED
    assert "не разрешено сценарием" in found.reason
    assert RESTORE_ID not in rows_of(result)


# --- S7 ----------------------------------------------------------------------------------------


def limit_row(result, path):
    return rows_of(result)[API.get("POST", path).id + "-LIMIT"]


def test_value_outside_limits_is_refused_and_unchanged(camera):
    assert limit_row(run(camera, allow_writes=True), BRIGHTNESS).status is Status.PASSED


def test_accepted_value_outside_limits_fails_and_is_returned(camera):
    camera.accepts_out_of_range = True
    before = camera.field_value(BRIGHTNESS, "brightness")
    found = limit_row(run(camera, allow_writes=True), BRIGHTNESS)
    assert found.status is Status.FAILED
    assert camera.field_value(BRIGHTNESS, "brightness") == before


# --- S8, S9 ------------------------------------------------------------------------------------


def test_taboo_never_sent_with_all_permissions(camera):
    result = run(camera, allow_writes=True, allow_destructive=True)
    for op in API.operations:
        if op.kind in (TABOO, NOT_CALLED):
            assert not [
                r
                for r in camera.sent(op.method, op.path)
                if op.path
                not in ("/api/auth/create_session", "/api/auth/login", "/api/auth/logout")
            ], op.path
            assert rows_of(result)[op.id].status is Status.SKIPPED


def test_taboo_row_reason(camera):
    found = row(run(camera), "POST", "/api/network/tcpip")
    assert found.status is Status.SKIPPED
    assert found.reason == "запрещено табу"


def test_destructive_not_sent_without_checkbox(camera):
    result = run(camera, allow_writes=True)
    for op in API.operations:
        if op.kind == DESTRUCTIVE:
            assert not camera.sent(op.method, op.path)
            assert "не разрешено сценарием" in rows_of(result)[op.id].reason


def test_reboot_with_checkbox_waits_for_the_camera(camera):
    result = run(camera, allow_destructive=True)
    assert camera.reboots == 1
    assert row(result, "POST", "/api/service/reboot").status is Status.PASSED
    assert camera.sessions == {}
    assert camera.requests[-1].path == "/api/auth/logout"


def test_records_are_not_removed_without_a_value(camera):
    result = run(camera, allow_destructive=True)
    assert not camera.sent("POST", "/api/recorder/remove_dir")
    assert "нужны параметры" in row(result, "POST", "/api/recorder/remove_dir").reason


# --- предел времени и остановка ----------------------------------------------------------------


def test_stop_returns_value_and_closes_session(camera):
    stop = {"now": False}

    def press_stop(mock, logged):
        if logged.method == "POST" and logged.path == BRIGHTNESS:
            stop["now"] = True

    camera.on_request = press_stop
    before = camera.field_value(BRIGHTNESS, "brightness")
    result = run(camera, allow_writes=True, stop_requested=lambda: stop["now"])
    assert camera.field_value(BRIGHTNESS, "brightness") == before
    assert camera.sessions == {}
    assert row(result, "POST", "/api/image/contrast").status is Status.SKIPPED
    assert not camera.sent("POST", "/api/image/contrast")
    assert rows_of(result)[RESTORE_ID].status is Status.PASSED


def test_time_limit_stops_new_writes(camera):
    result = run(camera, allow_writes=True, extra={"api_budget_s": 0})
    assert changing_requests(camera) == []
    assert camera.sessions == {}
    assert "не уложилась" in row(result, "POST", BRIGHTNESS).reason


def test_own_limit_is_below_the_scheduler_limit():
    assert ruver.budget_s(API) + ruver.RESERVE_S <= SPEC.timeout_s


# --- S10, S11 ----------------------------------------------------------------------------------


def test_only_failed_rows_become_tasks(camera):
    camera.corrupt.add(BRIGHTNESS)
    result = run(camera)
    statuses = {child.result.status for child in result.children}
    assert Status.FAILED in statuses
    assert [c.check_id for c in result.children if c.result.status is Status.FAILED] == [
        API.get("GET", BRIGHTNESS).id
    ]


@pytest.mark.parametrize("vendor, planned", [(RUVER, True), ("dahua", False), ("generic", False)])
def test_only_cameras_with_adapter_get_api_rows(vendor, planned):
    camera = Camera(slot=1, address="127.0.0.1", username="admin", vendor=vendor)
    plan = REGISTRY.plan_for(camera, Scenario(), Place.COLOR_BOX)
    assert (CHECK_ID in [spec.id for spec, _ in plan]) is planned


def test_write_kind_is_only_sent_as_a_write(camera):
    run(camera, allow_writes=True)
    writes = {op.path for op in API.operations if op.kind == WRITE}
    sent = {r.path for r in changing_requests(camera)}
    assert sent <= writes
