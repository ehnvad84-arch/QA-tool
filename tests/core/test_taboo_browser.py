"""Решения браузера веб-обхода: опасные действия, статика, окна входа и выхода (AD-7)."""

import json

import pytest

from qa_tool.core.taboo import (
    TabooViolation,
    WebDangerViolation,
    check_login_window,
    check_logout_window,
    check_web_danger,
    check_web_request,
    is_logout_label,
    is_static_asset,
    is_web_action,
    is_write_message,
    web_danger_word,
)

CAM = "http://10.0.0.1"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/sdcard/prepare", ""),
        ("POST", "/api/service/reboot", ""),
        ("GET", "/cgi-bin/sysreboot?now=1", ""),
        ("GET", "/cgi-bin/magicBox.cgi?action=shutdown", ""),
        ("POST", "/api/events/remove_all", ""),
        ("POST", "/api/image/reset_parameters", ""),
        ("POST", "/sd/format", ""),
        ("POST", "/cmd", '{"action": "format"}'),
        ("POST", "/cmd", "op=wipe"),
        ("POST", "/RPC2", '{"method": "storage.deleteAll"}'),
    ],
)
def test_danger_is_stopped(method, path, body):
    with pytest.raises(WebDangerViolation):
        check_web_danger(method, CAM + path, body)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/snapshot?format=jpeg", ""),
        ("GET", "/api/datetime?format=24", ""),
        ("POST", "/api/video/encoder", '{"format": "H264"}'),
        ("GET", "/api/ptz/wiper", ""),
        ("POST", "/api/ptz/wiper", '{"on": true}'),
        ("GET", "/api/service/is_reset", ""),
        ("GET", "/api/image/preset_mode", ""),
        ("POST", "/api/audio/stereo", '{"mode": "Стерео"}'),
    ],
)
def test_ordinary_requests_are_not_danger(method, path, body):
    check_web_danger(method, CAM + path, body)


def test_danger_list_does_not_touch_http_path():
    check_web_request("POST", CAM + "/api/sdcard/prepare", "")


@pytest.mark.parametrize(
    ("label", "danger"),
    [
        ("Перезагрузка", True),
        ("Reboot", True),
        ("Форматировать карту", True),
        ("Удалить", True),
        ("Очистить журнал", True),
        ("Формат даты", False),
        ("Стерео", False),
        ("Сохранить", False),
    ],
)
def test_danger_labels(label, danger):
    assert bool(web_danger_word(label)) is danger


@pytest.mark.parametrize(
    ("path", "static"),
    [
        ("/js/reboot.js", True),
        ("/css/edit.css", True),
        ("/index.html", True),
        ("/img/logo.png", True),
        ("/reboot.html", False),
        ("/api/image/brightness", False),
        ("/js/app.js?cmd=reboot", False),
    ],
)
def test_static_assets(path, static):
    assert is_static_asset("GET", CAM + path) is static


def test_post_is_never_static():
    assert not is_static_asset("POST", CAM + "/js/app.js")


@pytest.mark.parametrize(
    ("url", "action"),
    [
        ("/cgi-bin/ptz.cgi?code=Left", True),
        ("/api/stream/start", True),
        ("/api/image/brightness", False),
        ("/status.json", False),
    ],
)
def test_web_action(url, action):
    assert is_web_action("GET", CAM + url) is action


@pytest.mark.parametrize(
    ("text", "write"),
    [('{"cmd": "subscribe"}', True), ('{"action": "set"}', True), ('{"ping": 1}', False)],
)
def test_write_message(text, write):
    assert is_write_message(text) is write


def login(path="/api/auth/login", body="", password="network1"):
    check_login_window("POST", CAM + path, body, "admin", password)


def test_login_requests_pass():
    login(body=json.dumps({"username": "admin", "password": "network1"}))
    login("/api/auth/challenge", json.dumps({"username": "admin"}))
    login("/api/auth/validate_session", json.dumps({"sessionID": "12"}))
    login("/RPC2_Login", json.dumps({"method": "global.login", "params": {"userName": "admin"}}))
    login("/cgi-bin/login.cgi", "username=admin&password=network1")


def test_password_value_does_not_break_login():
    body = json.dumps({"username": "admin", "password": "network1"})
    login(body=body)
    login(body=json.dumps({"username": "admin", "password": "resetNew1"}), password="resetNew1")


def test_random_hash_does_not_break_login():
    body = json.dumps({"username": "admin", "password": "aB9SetxNewQ2oldZ7confirmK1pQ"})
    login(body=body, password="другой")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/users/set_password", ""),
        ("/change_password", ""),
        ("/login", json.dumps({"oldPassword": "a", "newPassword": "b"})),
        ("/login", json.dumps({"password": "a", "confirmPassword": "a"})),
        ("/login", "password=network1&password2=network1"),
        ("/login", json.dumps({"password": "network1", "repeat": "network1"})),
        ("/activate", json.dumps({"password": "network1"})),
        ("/forgot", ""),
        ("/login", json.dumps({"network": "dhcp"})),
        ("/api/network/login", ""),
    ],
)
def test_password_change_is_stopped_inside_window(path, body):
    with pytest.raises(TabooViolation):
        login(path, body)


def test_unreadable_body_is_stopped_inside_window():
    with pytest.raises(TabooViolation):
        check_login_window("POST", CAM + "/login", None, "admin", "x")


def test_read_passes_login_window():
    check_login_window("GET", CAM + "/api/auth/create_session", "", "admin", "x")


def test_logout_window():
    check_logout_window("POST", CAM + "/api/auth/logout", "")
    check_logout_window("POST", CAM + "/logout.cgi", "")
    with pytest.raises(TabooViolation):
        check_logout_window("POST", CAM + "/api/users/set_password", "")
    with pytest.raises(TabooViolation):
        check_logout_window("POST", CAM + "/logout", json.dumps({"ip": "1"}))


@pytest.mark.parametrize(
    ("label", "logout"),
    [
        ("Выйти", True),
        (" выйти ", True),
        ("Выход из системы", True),
        ("Log out", True),
        ("Logout", True),
        ("Sign out", True),
        ("Выход", False),
        ("Тревожный выход", False),
        ("Выход 1", False),
    ],
)
def test_logout_labels(label, logout):
    assert is_logout_label(label) is logout
