import pytest

from qa_tool.core.config import load_scenario, load_stand, load_thresholds, save_stand
from qa_tool.core.models import Camera, ConfigError, Place, Scenario, Stand


def _stand_yaml(tmp_path, cameras: str, place: str = "color_box"):
    path = tmp_path / "stand.yaml"
    path.write_text(f"place: {place}\nruns_dir: runs\ncameras:\n{cameras}", encoding="utf-8")
    return path


def _cameras(n: int) -> str:
    return "".join(
        f"  - {{slot: {i}, address: 10.0.0.{i}, username: admin}}\n" for i in range(1, n + 1)
    )


def test_stand_with_eight_cameras_loads(tmp_path):
    stand = load_stand(_stand_yaml(tmp_path, _cameras(8), place="dark_room"))
    assert [c.slot for c in stand.cameras] == list(range(1, 9))
    assert stand.place is Place.DARK_ROOM
    assert stand.runs_dir == tmp_path / "runs"


def test_ninth_camera_is_rejected(tmp_path):
    stand = load_stand(_stand_yaml(tmp_path, _cameras(8)))
    with pytest.raises(ConfigError, match="Свободных мест нет"):
        stand.add_camera(Camera(slot=1, address="10.0.0.9", username="admin"))


def test_slot_outside_range_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="допустимы места 1–8"):
        load_stand(_stand_yaml(tmp_path, "  - {slot: 9, address: 10.0.0.9, username: admin}\n"))


def test_duplicate_slot_is_rejected(tmp_path):
    cams = "  - {slot: 2, address: a, username: u}\n  - {slot: 2, address: b, username: u}\n"
    with pytest.raises(ConfigError, match="Место 2 уже занято"):
        load_stand(_stand_yaml(tmp_path, cams))


def test_address_behind_tunnel_survives_saving(tmp_path):
    camera = Camera(slot=1, address="127.0.0.1", port=9114, username="a", lan_address="10.0.0.5")
    path = tmp_path / "stand.yaml"
    save_stand(Stand(place=Place.COLOR_BOX, runs_dir=tmp_path, cameras=[camera]), path)
    assert load_stand(path).cameras[0].lan_address == "10.0.0.5"


def test_password_in_stand_yaml_is_config_error(tmp_path):
    cams = "  - {slot: 1, address: a, username: u, password: secret}\n"
    with pytest.raises(ConfigError, match="пароль в файле хранить нельзя"):
        load_stand(_stand_yaml(tmp_path, cams))


def test_unknown_place_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="неизвестное место"):
        load_stand(_stand_yaml(tmp_path, _cameras(1), place="garage"))


def test_missing_scenario_gives_safe_defaults(tmp_path):
    assert load_scenario(tmp_path / "scenario.yaml") == Scenario()
    assert load_scenario(None).allow_destructive is False


def test_scenario_loads_flags(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("checks: [CORE-CONNECT-1]\nallow_writes: true\n", encoding="utf-8")
    scenario = load_scenario(path)
    assert scenario.checks == ["CORE-CONNECT-1"]
    assert scenario.allow_writes and not scenario.allow_destructive


def test_threshold_without_source_is_rejected(tmp_path):
    path = tmp_path / "thresholds.yaml"
    path.write_text("daynight.switch_s: {value: 10, unit: с}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="не указан источник"):
        load_thresholds(path)


def test_threshold_loads_with_source(tmp_path):
    path = tmp_path / "thresholds.yaml"
    path.write_text(
        "daynight.switch_s: {value: 10, unit: с, source: стартовое}\n", encoding="utf-8"
    )
    t = load_thresholds(path)["daynight.switch_s"]
    assert (t.value, t.unit, t.source) == (10.0, "с", "стартовое")


@pytest.mark.parametrize("text", ["- 1\n- 2\n", "просто строка\n", "42\n"])
def test_yaml_not_a_mapping_is_config_error(tmp_path, text):
    path = tmp_path / "stand.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="ключ: значение"):
        load_stand(path)


@pytest.mark.parametrize(
    "text",
    ["password: x\ncameras: []\n", "cameras:\n  - {slot: 1, address: a, username: u, pwd: x}\n"],
)
def test_password_under_any_name_rejected(tmp_path, text):
    path = tmp_path / "stand.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="пароль"):
        load_stand(path)
