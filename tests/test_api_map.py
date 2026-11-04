"""Карта API «Рувер»: вид каждой операции задают закрытые списки, записанные здесь руками.

Сверка идёт по файлу карты в репозитории, а не по описанию прошивки: его в CI нет.
"""

import json

import pytest

from qa_tool.core.taboo import TabooViolation, check_http, check_web_request
from qa_tool.drivers.camera.ruver.client import (
    DESTRUCTIVE,
    MAP_FILE,
    NOT_CALLED,
    READ,
    RETURN_ONLY,
    TABOO,
    WRITE,
    load_map,
)

API = load_map()
POST_READS = {
    "/api/recorder/records",
    "/api/recorder/record_details",
    "/api/recorder/download",
    "/api/service/need_reboot",
    "/api/service/log",
    "/api/service/export_settings",
    "/api/video/snapshot",
}
GET_NOT_READS = {
    "/api/auth/create_session",
    "/api/auth/permissions",
    "/api/users/get_password",
    "/api/service/is_reset",
}


def of_kind(kind):
    return [op for op in API.operations if op.kind == kind]


def test_only_listed_posts_are_reads():
    assert {op.path for op in of_kind(READ) if op.method == "POST"} == POST_READS


def test_only_listed_gets_are_not_reads():
    assert {op.path for op in API.operations if op.method == "GET" and op.kind != READ} == (
        GET_NOT_READS
    )


def test_write_has_read_at_the_same_address():
    for op in of_kind(WRITE):
        assert API.get("GET", op.path).kind == READ, op.path


def test_write_has_a_value_that_can_be_chosen():
    for op in of_kind(WRITE):
        assert op.setting and (op.boolean or op.values or op.range_field), op.path


def test_return_only_list_is_closed():
    assert {op.path for op in of_kind(RETURN_ONLY)} == {"/api/osd/set_block_position"}


def test_destructive_list_is_closed():
    assert {op.path for op in of_kind(DESTRUCTIVE)} == {
        "/api/service/reboot",
        "/api/sdcard/prepare",
        "/api/recorder/remove_dir",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/image/reset_parameters",
        "/api/image/reset_all_parameters",
        "/api/events/remove_all",
        "/api/ptz/tours",
        "/api/ptz/move",
        "/api/ptz/calibrate",
        "/api/glonass/synchronize",
        "/api/info",
        "/api/auth/login",
    ],
)
def test_operations_without_undo_are_not_called(path):
    assert API.get("POST", path).kind == NOT_CALLED


@pytest.mark.parametrize(
    "path",
    [
        "/api/network/tcpip",
        "/api/users/set_password",
        "/api/snmp/add_account",
        "/api/lte/apn",
        "/api/datetime",
        "/api/datetime/timezone",
        "/api/video/multicast_ip",
        "/api/video/multicast_enable",
        "/api/service/reset",
        "/api/service/import_settings",
    ],
)
def test_network_accounts_and_time_are_taboo(path):
    assert API.get("POST", path).kind == TABOO


def test_every_taboo_operation_is_stopped_by_the_taboo_itself():
    for op in of_kind(TABOO):
        with pytest.raises(TabooViolation):
            check_http(op.method, op.path)


def test_everything_the_program_calls_passes_the_taboo():
    for op in [*of_kind(READ), *of_kind(WRITE), *of_kind(DESTRUCTIVE), *of_kind(RETURN_ONLY)]:
        check_http(op.method, op.path)
        body = {"id": 1, "x": 1, "y": 1} if op.kind == RETURN_ONLY else {op.setting: 1}
        check_web_request(op.method, op.path, json.dumps(op.body or body))


def test_auth_addresses_stay_out_of_the_walk():
    auth = [op for op in API.operations if op.path.startswith("/api/auth/")]
    assert auth and all(op.kind == NOT_CALLED for op in auth)


def test_row_ids_are_unique():
    ids = [op.id for op in API.operations]
    assert len(ids) == len(set(ids))


def test_vendor_text_does_not_get_into_the_repository():
    text = MAP_FILE.read_text(encoding="utf-8")
    assert '"description"' not in text and '"example"' not in text


def test_answers_with_possible_secrets_are_closed():
    for path in ("/api/configuration", "/api/users", "/api/snmp", "/api/lte"):
        assert API.get("GET", path) is None or API.get("GET", path).sensitive, path
    assert API.get("POST", "/api/service/export_settings").sensitive
    assert API.get("POST", "/api/service/log").sensitive
