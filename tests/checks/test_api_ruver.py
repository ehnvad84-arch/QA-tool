import json

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.api import ruver
from qa_tool.checks.api.ruver import CHECK_ID, LEFT_CHANGED, RESTORE_ID, changed_fields
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
    extra = {
        "api_timeout_s": 0.3,
        "api_reboot_poll_s": 0.01,
        "api_outage_watch_s": 0,  # обрыв после экспорта смотрят только его тесты
        "api_outage_poll_s": 0.01,
        **options.pop("extra", {}),
    }
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


def test_function_absent_from_the_model_is_skipped(camera):
    camera.state["/api/hardware"]["lteSupport"] = False
    found = row(run(camera), "GET", "/api/lte/enable")
    assert found.status is Status.SKIPPED
    assert "нет у этой модели" in found.reason
    assert not camera.sent("GET", "/api/lte/enable")


def test_absent_function_is_not_written(camera):
    camera.state["/api/hardware"]["glonassSupport"] = False
    found = row(run(camera, allow_writes=True), "POST", "/api/osd/glonass_enable")
    assert "нет у этой модели" in found.reason
    assert not camera.sent("POST", "/api/osd/glonass_enable")


def test_hardware_flags_work_even_if_its_answer_breaks_the_schema(camera):
    camera.state["/api/hardware"]["lteSupport"] = False
    camera.state["/api/hardware"]["manufacturer"] = 3
    result = run(camera)
    assert row(result, "GET", "/api/hardware").status is Status.FAILED
    assert row(result, "GET", "/api/lte").status is Status.SKIPPED


def test_export_restarting_the_api_is_its_own_failure(camera):
    camera.restarts_on_export = True
    result = run(camera, extra={"api_outage_watch_s": 1, "api_outage_poll_s": 0.02})
    found = row(result, "POST", "/api/service/export_settings")
    assert found.status is Status.FAILED
    assert "служба перезапустилась" in found.reason
    others = [c for c in result.children if c.result.status is Status.STAND_ERROR]
    assert others == []


def test_export_without_restart_passes(camera):
    result = run(camera, extra={"api_outage_watch_s": 0.05, "api_outage_poll_s": 0.01})
    assert row(result, "POST", "/api/service/export_settings").status is Status.PASSED


def test_export_runs_after_writes_are_verified(camera):
    run(camera, allow_writes=True, extra={"api_outage_watch_s": 0.05, "api_outage_poll_s": 0.01})
    paths = [r.path for r in camera.requests]
    assert paths.index("/api/service/export_settings") > max(
        i for i, r in enumerate(camera.requests) if r.method == "POST" and r.path == BRIGHTNESS
    )


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


def test_busy_camera_gets_a_second_return_attempt(camera):
    camera.refuses_next_write_once.add(BRIGHTNESS)
    before = camera.field_value(BRIGHTNESS, "brightness")
    found = row(run(camera, allow_writes=True, extra={"api_return_pause_s": 0}), "POST", BRIGHTNESS)
    assert found.status is Status.PASSED
    assert camera.field_value(BRIGHTNESS, "brightness") == before


def test_failed_relogin_after_write_is_reported_as_left_changed(camera):
    def lose_session(mock, logged):
        if logged.method == "POST" and logged.path == BRIGHTNESS:
            mock.sessions.clear()
            mock.busy_sessions = 3

    camera.on_request = lose_session
    result = run(camera, allow_writes=True)
    assert result.children
    assert LEFT_CHANGED in row(result, "POST", BRIGHTNESS).reason
    assert camera.login_attempts == 1


def test_setting_with_answer_outside_the_schema_is_still_verified(camera):
    contrast = "/api/image/contrast"
    del camera.state[contrast]["contrastRange"]
    before = camera.field_value(contrast, "contrast")
    camera.side_effects[BRIGHTNESS] = (contrast, "contrast", before + 7)
    result = run(camera, allow_writes=True)
    assert row(result, "GET", contrast).status is Status.FAILED
    assert camera.field_value(contrast, "contrast") == before
    assert contrast in rows_of(result)[RESTORE_ID].actual


def test_repeated_or_odd_ids_do_not_break_comparison():
    assert changed_fields([{"id": 1, "x": 1}, {"id": 1, "x": 2}], [{"id": 1, "x": 1}]) != []
    assert changed_fields([{"id": [1], "x": 1}], [{"id": [1], "x": 1}]) == []


def test_order_of_blocks_is_not_a_change(camera):
    blocks = [{"id": 1, "x": 5, "y": 6}, {"id": 2, "x": 7, "y": 8}]
    assert changed_fields({"b": blocks}, {"b": blocks[::-1]}) == []


def test_moved_block_is_named():
    old = [{"id": 1, "x": 5}, {"id": 2, "x": 7}]
    new = [{"id": 2, "x": 7}, {"id": 1, "x": 9}]
    assert changed_fields({"b": old}, {"b": new}) == ["b[1].x"]


def test_null_answer_is_not_unread(camera):
    camera.state["/api/detector/roi"] = None
    result = run(camera, allow_writes=True)
    assert rows_of(result)[RESTORE_ID].status is Status.PASSED


def test_moved_block_is_returned_and_stays_a_task(camera):
    path = "/api/osd/block_positions"
    block = camera.state[path]["overlayBlocks"][0]
    block |= {"id": 1, "x": 1503, "y": 1019}
    moved = [{**block, "x": 844}]
    camera.side_effects[BRIGHTNESS] = (path, "overlayBlocks", moved)
    result = run(camera, allow_writes=True)
    assert moved[0]["x"] == 1503
    assert rows_of(result)[RESTORE_ID].status is Status.PASSED
    found = row(result, "POST", "/api/osd/set_block_position")
    assert found.status is Status.FAILED
    assert "сдвинулись надписи" in found.reason


def test_block_is_never_touched_without_a_move(camera):
    result = run(camera, allow_writes=True)
    assert not camera.sent("POST", "/api/osd/set_block_position")
    assert row(result, "POST", "/api/osd/set_block_position").status is Status.SKIPPED


def test_value_returned_only_by_verify_is_not_left_changed(camera):
    camera.refuses_next_write_once.add(BRIGHTNESS)
    camera.busy_writes_left = 2  # обе попытки возврата отказаны, проходит только сверка
    before = camera.field_value(BRIGHTNESS, "brightness")
    result = run(camera, allow_writes=True, extra={"api_return_pause_s": 0})
    found = row(result, "POST", BRIGHTNESS)
    assert found.status is Status.FAILED
    assert LEFT_CHANGED not in found.reason
    assert "вернулось только при сверке" in found.reason
    assert camera.field_value(BRIGHTNESS, "brightness") == before
    assert row_id_absent_or_skipped(result, BRIGHTNESS)


def row_id_absent_or_skipped(result, path):
    found = rows_of(result).get(API.get("POST", path).id + "-LIMIT")
    return found is None or found.status is Status.SKIPPED


def test_lost_answer_outside_limits_still_returns_value(camera):
    camera.drops_answer_for = lambda logged: (
        logged.method == "POST" and logged.path == BRIGHTNESS and logged.body["brightness"] == 101
    )
    camera.accepts_out_of_range = True
    before = camera.field_value(BRIGHTNESS, "brightness")
    found = limit_row(run(camera, allow_writes=True), BRIGHTNESS)
    assert found.status is Status.STAND_ERROR
    assert camera.field_value(BRIGHTNESS, "brightness") == before


def test_address_whose_first_read_failed_is_not_called_matching(camera):
    contrast = "/api/image/contrast"
    camera.forbidden.add(contrast)
    camera.on_request = lambda mock, logged: (
        mock.forbidden.discard(contrast) if logged.path == "/api/image/saturation" else None
    )
    found = rows_of(run(camera, allow_writes=True))[RESTORE_ID]
    assert found.status is Status.STAND_ERROR
    assert "сверка не закончена" in found.reason


def block(number, x):
    return {"id": number, "x": x, "y": 1019, "width": 407, "height": 56}


def test_block_moved_on_another_stream_is_returned(camera):
    camera.blocks_by_stream["secondary"] = [block(1, 900)]

    def move(mock, logged):
        if logged.method == "POST" and logged.path == BRIGHTNESS:
            mock.blocks_by_stream["secondary"][0]["x"] = 300

    camera.on_request = move
    result = run(camera, allow_writes=True)
    assert camera.blocks_by_stream["secondary"][0]["x"] == 900
    assert "secondary" in row(result, "POST", "/api/osd/set_block_position").reason
    sent = camera.sent("POST", "/api/osd/set_block_position")
    assert [r.query for r in sent] == ["stream_id=secondary"]


def test_block_that_cannot_be_returned_is_left_changed(camera):
    camera.blocks_by_stream["secondary"] = [block(1, 900)]
    camera.refuses_block_return = True

    def move(mock, logged):
        if logged.method == "POST" and logged.path == BRIGHTNESS:
            mock.blocks_by_stream["secondary"][0]["x"] = 300

    camera.on_request = move
    found = rows_of(run(camera, allow_writes=True))[RESTORE_ID]
    assert found.status is Status.FAILED
    assert LEFT_CHANGED in found.reason


def test_stream_with_failed_snapshot_is_not_called_matching(camera):
    camera.blocks_by_stream["secondary"] = [block(1, 900)]
    dropped = []

    def drop_first_read(logged):
        wanted = logged.path == "/api/osd/block_positions" and "secondary" in logged.query
        if wanted and not dropped:
            dropped.append(logged)
            return True
        return False

    camera.drops_answer_for = drop_first_read
    found = rows_of(run(camera, allow_writes=True))[RESTORE_ID]
    assert found.status is Status.STAND_ERROR
    assert "сверка не закончена" in found.reason


def test_stream_the_camera_does_not_have_is_not_a_problem(camera):
    camera.absent_streams.add("preview")
    assert rows_of(run(camera, allow_writes=True))[RESTORE_ID].status is Status.PASSED


def test_block_without_coordinates_does_not_break_the_walk(camera):
    camera.blocks_by_stream["secondary"] = [{"id": 1}]
    assert rows_of(run(camera, allow_writes=True))[RESTORE_ID].status is Status.PASSED


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


def test_reboot_that_did_not_happen_is_not_a_pass(camera):
    camera.fake_reboot = True
    extra = {"api_reboot_down_s": 0.05}
    found = row(run(camera, allow_destructive=True, extra=extra), "POST", "/api/service/reboot")
    assert found.status is Status.FAILED
    assert "не ушла в перезагрузку" in found.reason


def test_lost_answer_without_reboot_is_stand_error(camera):
    camera.fake_reboot = True
    camera.drops_reboot_answer = True
    extra = {"api_reboot_down_s": 0.05}
    found = row(run(camera, allow_destructive=True, extra=extra), "POST", "/api/service/reboot")
    assert found.status is Status.STAND_ERROR


def test_one_lost_answer_is_not_a_reboot(camera):
    camera.fake_reboot = True
    lost = []

    def lose_one_info(logged):
        after_reboot = camera.sent("POST", "/api/service/reboot")
        if after_reboot and logged.path == "/api/info" and not lost:
            lost.append(logged)
            return True
        return False

    camera.drops_answer_for = lose_one_info
    extra = {"api_reboot_down_s": 0.1}
    found = row(run(camera, allow_destructive=True, extra=extra), "POST", "/api/service/reboot")
    assert lost
    assert found.status is Status.FAILED
    assert "не ушла в перезагрузку" in found.reason


def test_camera_that_never_returns_after_reboot_fails(camera):
    camera.down_requests = 10**6
    extra = {"api_reboot_wait_s": 0.2}
    found = row(run(camera, allow_destructive=True, extra=extra), "POST", "/api/service/reboot")
    assert found.status is Status.FAILED
    assert "не вернулась" in found.reason


def test_reboot_without_answer_still_waits_for_the_camera(camera):
    camera.drops_reboot_answer = True
    result = run(camera, allow_destructive=True)
    assert row(result, "POST", "/api/service/reboot").status is Status.PASSED


def test_export_then_reboot_in_one_run(camera):
    camera.restarts_on_export = True
    extra = {"api_outage_watch_s": 1, "api_outage_poll_s": 0.02, "api_reboot_wait_s": 2}
    result = run(camera, allow_destructive=True, extra=extra)
    assert row(result, "POST", "/api/service/reboot").status is Status.PASSED


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
