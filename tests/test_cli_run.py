import pytest

from qa_tool.cli import EXIT_FAILED, EXIT_OK, EXIT_STAND, main
from tests.mock_camera import stand_yaml, start_fleet


@pytest.fixture
def fleet():
    cameras, responder = start_fleet(8, with_discovery=True)
    yield cameras
    responder.stop()
    for cam in cameras:
        cam.stop()


def write_stand(tmp_path, fleet):
    path = tmp_path / "stand.yaml"
    path.write_text(stand_yaml(fleet), encoding="utf-8")
    (tmp_path / "scenario.yaml").write_text("checks: [CORE-CONNECT-1]\n", encoding="utf-8")
    return path


def set_passwords(monkeypatch, wrong_slot=None):
    for slot in range(1, 9):
        monkeypatch.setenv(f"QA_TOOL_PASSWORD_{slot}", "bad" if slot == wrong_slot else "admin")


def test_run_all_passed_creates_run_folder(tmp_path, fleet, monkeypatch, capsys):
    set_passwords(monkeypatch)
    assert main(["run", "--stand", str(write_stand(tmp_path, fleet))]) == EXIT_OK
    runs = list((tmp_path / "runs").iterdir())
    assert len(runs) == 1 and (runs[0] / "run.sqlite").exists()
    assert "место 8: CORE-CONNECT-1 прошла" in capsys.readouterr().out


def test_run_with_failed_check_exits_1(tmp_path, fleet, monkeypatch):
    set_passwords(monkeypatch, wrong_slot=4)
    assert main(["run", "--stand", str(write_stand(tmp_path, fleet))]) == EXIT_FAILED


def test_config_error_exits_2(tmp_path, fleet, monkeypatch, capsys):
    path = write_stand(tmp_path, fleet)  # паролей нет ни в окружении, ни в хранилище
    assert main(["run", "--stand", str(path)]) == EXIT_STAND
    assert "не задан пароль" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_discover_lists_cameras(fleet, capsys):
    assert main(["discover", "--interface", "127.0.0.1", "--timeout", "2"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "MOCK-1" in out and "Ruver" in out
