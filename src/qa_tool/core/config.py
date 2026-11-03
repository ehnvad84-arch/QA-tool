"""Чтение stand.yaml, scenario.yaml и thresholds.yaml."""

from pathlib import Path
from typing import Any

import yaml

from qa_tool.core.models import Camera, ConfigError, Place, Scenario, Stand, Threshold

_FORBIDDEN_CAMERA_KEYS = {"password", "пароль"}


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"Нет файла {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: ошибка разметки YAML — {e}") from e


def load_stand(path: Path) -> Stand:
    data = _read_yaml(path)
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
        if _FORBIDDEN_CAMERA_KEYS & set(raw):
            raise ConfigError(
                f"{path.name}, место {raw.get('slot')}: пароль в файле хранить нельзя — "
                "он задаётся в программе и лежит в системном хранилище паролей"
            )
        try:
            cameras.append(Camera(**raw))
        except TypeError as e:
            raise ConfigError(f"{path.name}, место {raw.get('slot')}: {e}") from e
    return Stand(place=place, runs_dir=runs_dir, cameras=cameras)


def load_scenario(path: Path | None) -> Scenario:
    if path is None or not path.exists():
        return Scenario()
    data = _read_yaml(path)
    try:
        return Scenario(**data)
    except TypeError as e:
        raise ConfigError(f"{path.name}: {e}") from e


def load_thresholds(path: Path) -> dict[str, Threshold]:
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
