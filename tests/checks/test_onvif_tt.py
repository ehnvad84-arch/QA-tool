import os
import subprocess
import sys

import pytest

from qa_tool.checks import load_all
from qa_tool.checks.onvif import onvif_tt
from qa_tool.checks.onvif.excluded import EXCLUDED
from qa_tool.core.checks import REGISTRY, CheckContext, Status, run_check
from qa_tool.core.models import Camera, Place
from qa_tool.core.taboo import FORBIDDEN_ONVIF
from tests.mock_camera import start_fleet

load_all()
SPEC = REGISTRY.get(onvif_tt.CHECK_ID)


@pytest.fixture
def mock():
    cameras, _ = start_fleet(1)
    yield cameras[0]
    cameras[0].stop()


def ctx_for(mock, **extra):
    camera = Camera(
        slot=1, address="127.0.0.1", port=mock.port, rtsp_port=mock.rtsp_port, username="admin"
    )
    return CheckContext(camera, "admin", Place.COLOR_BOX, extra=extra)


def test_onvif_tt_runs_through_proxy_one_row_per_test(mock):
    result = run_check(SPEC, ctx_for(mock))
    ids = {c.check_id for c in result.children}
    assert len(ids) > 100  # 161 тест onvif-tt: три исключены и добавлены строками «пропущена»
    assert set(EXCLUDED) <= ids
    statuses = {c.result.status for c in result.children}
    assert Status.PASSED in statuses and Status.SKIPPED in statuses
    excluded = [c for c in result.children if c.check_id in EXCLUDED]
    assert all("исключён в QA-tool" in c.result.reason for c in excluded)
    assert mock.requests, "onvif-tt должен был дойти до камеры через посредника"
    assert not {op.lower() for op in mock.requests} & FORBIDDEN_ONVIF


def test_password_only_in_environment(mock, monkeypatch):
    seen = {}

    def fake_run(command, env, **kwargs):
        seen["command"], seen["env"] = command, env
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(onvif_tt.subprocess, "run", fake_run)
    ctx = ctx_for(mock)
    ctx.password = "s3cret-pw"
    run_check(SPEC, ctx)
    assert "s3cret-pw" not in " ".join(seen["command"])
    assert seen["env"][onvif_tt.PASSWORD_ENV] == "s3cret-pw"
    assert seen["env"]["PATH"] == onvif_tt.path_without_ffprobe(os.environ.get("PATH", ""))
    assert "--allow-writes" not in seen["command"] and "--allow-reboot" not in seen["command"]


@pytest.mark.parametrize(
    ("writes", "destructive", "expected"),
    [
        (False, True, []),
        (True, False, ["--allow-writes"]),
        (True, True, ["--allow-writes", "--allow-reboot"]),
    ],
)
def test_reboot_only_with_both_flags(mock, monkeypatch, writes, destructive, expected):
    seen = {}
    monkeypatch.setattr(
        onvif_tt.subprocess,
        "run",
        lambda command, **k: (
            seen.setdefault("c", command) and subprocess.CompletedProcess(command, 0)
        ),
    )
    ctx = ctx_for(mock)
    ctx.allow_writes, ctx.allow_destructive = writes, destructive
    run_check(SPEC, ctx)
    assert [a for a in seen["c"] if a.startswith("--allow")] == expected


def test_timeout_and_missing_report_are_stand_errors(mock, monkeypatch):
    result = run_check(SPEC, ctx_for(mock, onvif_tt_timeout_s=0.01))
    assert result.status is Status.STAND_ERROR and "не уложился" in result.reason
    monkeypatch.setattr(
        onvif_tt, "runner_command", lambda: [sys.executable, "-c", "import sys; sys.exit(3)"]
    )
    result = run_check(SPEC, ctx_for(mock))
    assert result.status is Status.STAND_ERROR and "не выдал отчёт" in result.reason


def test_taboo_stop_and_xfail_mapping():
    names = {"DEVICE-9-9-9": "set user"}
    stopped = onvif_tt.to_child(
        {
            "id": "DEVICE-9-9-9",
            "status": "failed",
            "longrepr": "E   Fault: ter:QAToolTaboo SetUser",
            "last_request": "<SetUser/>",
            "last_response": "<ter:QAToolTaboo/>",
        },
        names,
    )
    assert stopped.result.status is Status.STAND_ERROR
    assert stopped.result.reason.startswith("запрещённая операция не отправлена")
    xfail = onvif_tt.to_child({"id": "X-1", "status": "xfailed", "longrepr": "E   boom"}, names)
    assert (
        xfail.result.status is Status.FAILED and "известная ошибка прошивки" in xfail.result.reason
    )
    xpass = onvif_tt.to_child({"id": "X-2", "status": "xpassed", "last_request": "<a/>"}, names)
    assert xpass.result.status is Status.PASSED


def test_writes_snapshot_and_restore_row(mock, monkeypatch):
    def spoil_and_report(command, env, **kwargs):  # «onvif-tt» меняет время и пишет отчёт
        assert "--allow-writes" in command
        mock.state.datetime_type, mock.state.tz = "Manual", "UTC0"
        report = command[command.index("--report") + 1]
        with open(report, "w", encoding="utf-8") as f:
            f.write(
                '{"summary": {}, "results": [{"id": "DEVICE-3-1-11", "status": "passed", '
                '"last_request": "<SetSystemDateAndTime/>"}]}'
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(onvif_tt.subprocess, "run", spoil_and_report)
    ctx = ctx_for(mock)
    ctx.allow_writes = True
    result = run_check(SPEC, ctx)
    rows = {c.check_id: c.result for c in result.children}
    assert rows[onvif_tt.RESTORE_ID].status is Status.PASSED
    assert (mock.state.datetime_type, mock.state.tz) == ("NTP", "MSK-3")


def test_restore_happens_even_when_run_hangs_and_failure_is_visible(mock, monkeypatch):
    def hang(command, env, timeout, **kwargs):
        mock.state.datetime_type = "Manual"
        mock.state.reject_set = True  # и камера ещё и не даёт вернуть
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(onvif_tt.subprocess, "run", hang)
    ctx = ctx_for(mock)
    ctx.allow_writes = True
    result = run_check(SPEC, ctx)
    rows = {c.check_id: c.result for c in result.children}
    assert rows[onvif_tt.CHECK_ID].status is Status.STAND_ERROR
    restore = rows[onvif_tt.RESTORE_ID]
    assert restore.status is Status.STAND_ERROR and "камера осталась изменённой" in restore.reason


def test_no_snapshot_no_writes(mock, monkeypatch):
    seen = {}

    def fake_run(command, env, **kwargs):
        seen["c"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(onvif_tt.subprocess, "run", fake_run)
    monkeypatch.setattr(
        onvif_tt.SnapshotTaker,
        "take",
        lambda self: onvif_tt.Snapshot(skipped=["дата и время: нет ответа"]),
    )
    ctx = ctx_for(mock)
    ctx.allow_writes = True
    result = run_check(SPEC, ctx)
    assert "--allow-writes" not in seen["c"]
    rows = {c.check_id: c.result for c in result.children}
    assert "изменяющие тесты onvif-tt не запускались" in rows[onvif_tt.SNAPSHOT_ID].reason


def test_write_skip_reason_in_russian():
    row = onvif_tt.to_child(
        {
            "id": "PTZ-3-1-1",
            "status": "skipped",
            "longrepr": "Skipped: test mutates DUT state — pass --allow-writes to enable",
        },
        {},
    )
    assert row.result.status is Status.SKIPPED
    assert row.result.reason == "не разрешено сценарием: изменяющая проверка"


def test_every_onvif_tt_test_running_ffprobe_is_excluded():
    from onvif_tt import registry

    registry.discover()
    uses_ffprobe = {i for i, impl in registry.REGISTRY.items() if "requires_ffprobe" in impl.tags}
    assert uses_ffprobe and uses_ffprobe <= set(EXCLUDED)  # новая версия onvif-tt — сверить


def test_path_for_onvif_tt_has_no_ffprobe(tmp_path):
    with_ffprobe, plain = tmp_path / "ff", tmp_path / "plain"
    with_ffprobe.mkdir(), plain.mkdir()
    (with_ffprobe / ("ffprobe.exe" if os.name == "nt" else "ffprobe")).write_text("")
    path = os.pathsep.join([str(with_ffprobe), str(plain)])
    assert onvif_tt.path_without_ffprobe(path) == str(plain)
