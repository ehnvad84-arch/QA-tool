"""Чтение stand.yaml, scenario.yaml и thresholds.yaml."""

from pathlib import Path
from typing import Any

import yaml

from qa_tool.core.models import Camera, ConfigError, Place, Scenario, Stand, Threshold

_FORBIDDEN_CAMERA_KEYS = {"password", "passwd", "pwd", "pass", "secret", "пароль"}


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"Нет файла {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: ошибка разметки YAML — {e}") from e


def load_stand(path: Path) -> Stand:
    """Прочитать stand.yaml.

    Raises:
        ConfigError: нет файла, ошибка YAML, неизвестное место стенда, неверное место
            камеры или пароль в файле (пароли — только в хранилище паролей).
    """
    data = _read_yaml(path)
    if _FORBIDDEN_CAMERA_KEYS & {str(k).lower() for k in data}:
        raise ConfigError(f"{path.name}: пароль в файле стенда хранить нельзя")
    try:
        place = Place(data.get("place", Place.COLOR_BOX))
    except ValueError as e:
        allowed = ", ".join(p.value for p in Place)
        raise ConfigError(f"{path.name}: неизвестное место стенда, допустимо: {allowed}") from e
    runs_dir = Path(data.get("runs_dir", "runs"))
    if not runs_dir.is_absolute():
        runs_dir = path.parent / runs_dir
    cameras = []
    for raw in data.get("cameras") or []:
        if _FORBIDDEN_CAMERA_KEYS & {str(k).lower() for k in raw}:
            raise ConfigError(
                f"{path.name}, место {raw.get('slot')}: пароль в файле хранить нельзя — "
                "он задаётся в программе и лежит в системном хранилище паролей"
            )
        try:
            cameras.append(Camera(**raw))
        except TypeError as e:
            raise ConfigError(f"{path.name}, место {raw.get('slot')}: {e}") from e
    return Stand(
        place=place, runs_dir=runs_dir, cameras=cameras, interface=str(data.get("interface", ""))
    )


def load_scenario(path: Path | None) -> Scenario:
    """Прочитать scenario.yaml; нет файла — безопасный сценарий по умолчанию."""
    if path is None or not path.exists():
        return Scenario()
    data = _read_yaml(path)
    try:
        return Scenario(**data)
    except TypeError as e:
        raise ConfigError(f"{path.name}: {e}") from e


def load_thresholds(path: Path) -> dict[str, Threshold]:
    """Прочитать thresholds.yaml; у каждого порога обязателен источник значения.

    Raises:
        ConfigError: нет источника или value не число.
    """
    thresholds = {}
    for key, raw in _read_yaml(path).items():
        if not raw.get("source"):
            raise ConfigError(f"{path.name}, порог {key}: не указан источник значения")
        try:
            thresholds[key] = Threshold(
                value=float(raw["value"]), unit=raw.get("unit", ""), source=raw["source"]
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"{path.name}, порог {key}: нужно числовое value") from e
    return thresholds


def save_stand(stand: Stand, path: Path) -> None:
    """Записать stand.yaml. Паролей в нём нет и быть не может (security.md)."""
    runs_dir = stand.runs_dir
    if runs_dir.is_relative_to(path.parent):
        runs_dir = runs_dir.relative_to(path.parent)
    data = {
        "place": stand.place.value,
        "runs_dir": runs_dir.as_posix(),
        "interface": stand.interface,
        "cameras": [
            {
                "slot": c.slot,
                "address": c.address,
                "port": c.port,
                "rtsp_port": c.rtsp_port,
                "username": c.username,
                "vendor": c.vendor,
                "name": c.name,
            }
            for c in stand.cameras
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def save_scenario(scenario: Scenario, path: Path) -> None:
    """Записать scenario.yaml рядом со stand.yaml."""
    from dataclasses import asdict

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(asdict(scenario), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
